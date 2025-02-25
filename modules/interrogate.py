import contextlib
import os
import sys
import traceback
from collections import namedtuple
import re

import torch

from torchvision import transforms
from torchvision.transforms.functional import InterpolationMode

import modules.shared as shared
from modules import devices, paths, lowvram

blip_image_eval_size = 384
blip_local_dir = os.path.join('models', 'Interrogator')
blip_local_file = os.path.join(blip_local_dir, 'model_base_caption_capfilt_large.pth')
blip_model_url = 'https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_base_caption_capfilt_large.pth'
clip_model_name = 'ViT-L/14'

Category = namedtuple("Category", ["name", "topn", "items"])

re_topn = re.compile(r"\.top(\d+)\.")


class InterrogateModels:
    blip_model = None
    clip_model = None
    clip_preprocess = None
    categories = None
    dtype = None
    running_on_cpu = None

    def __init__(self, content_dir):
        self.categories = []
        self.running_on_cpu = devices.device_interrogate == torch.device("cpu")

        if os.path.exists(content_dir):
            for filename in os.listdir(content_dir):
                m = re_topn.search(filename)
                topn = 1 if m is None else int(m.group(1))

                with open(os.path.join(content_dir, filename), "r", encoding="utf8") as file:
                    lines = [x.strip() for x in file.readlines()]

                self.categories.append(Category(name=filename, topn=topn, items=lines))

    def load_blip_model(self):
        import models.blip

        if not os.path.isfile(blip_local_file):
            if not os.path.isdir(blip_local_dir):
                os.mkdir(blip_local_dir)

            print("Downloading BLIP...")
            from requests import get as reqget
            open(blip_local_file, 'wb').write(reqget(blip_model_url, allow_redirects=True).content)
            print("BLIP downloaded to", blip_local_file + '.')

        blip_model = models.blip.blip_decoder(pretrained=blip_local_file, image_size=blip_image_eval_size, vit='base', med_config=os.path.join(paths.paths["BLIP"], "configs", "med_config.json"))
        blip_model.eval()

        return blip_model

    def load_clip_model(self):
        import clip

        if self.running_on_cpu:
            model, preprocess = clip.load(clip_model_name, device="cpu", download_root=shared.cmd_opts.clip_models_path)
        else:
            model, preprocess = clip.load(clip_model_name, download_root=shared.cmd_opts.clip_models_path)

        model.eval()
        model = model.to(devices.device_interrogate)

        return model, preprocess

    def load(self):
        if self.blip_model is None:
            self.blip_model = self.load_blip_model()
            if not shared.cmd_opts.no_half and not self.running_on_cpu:
                self.blip_model = self.blip_model.half()

        self.blip_model = self.blip_model.to(devices.device_interrogate)

        if self.clip_model is None:
            self.clip_model, self.clip_preprocess = self.load_clip_model()
            if not shared.cmd_opts.no_half and not self.running_on_cpu:
                self.clip_model = self.clip_model.half()

        self.clip_model = self.clip_model.to(devices.device_interrogate)

        self.dtype = next(self.clip_model.parameters()).dtype

    def send_clip_to_ram(self):
        if not shared.opts.interrogate_keep_models_in_memory:
            if self.clip_model is not None:
                self.clip_model = self.clip_model.to(devices.cpu)

    def send_blip_to_ram(self):
        if not shared.opts.interrogate_keep_models_in_memory:
            if self.blip_model is not None:
                self.blip_model = self.blip_model.to(devices.cpu)

    def unload(self):
        self.send_clip_to_ram()
        self.send_blip_to_ram()

        devices.torch_gc()

    def rank(self, image_features, text_array, top_count=1):
        import clip

        if shared.opts.interrogate_clip_dict_limit != 0:
            text_array = text_array[0:int(shared.opts.interrogate_clip_dict_limit)]

        top_count = min(top_count, len(text_array))
        text_tokens = clip.tokenize([text for text in text_array], truncate=True).to(devices.device_interrogate)
        text_features = self.clip_model.encode_text(text_tokens).type(self.dtype)
        text_features /= text_features.norm(dim=-1, keepdim=True)

        similarity = torch.zeros((1, len(text_array))).to(devices.device_interrogate)
        for i in range(image_features.shape[0]):
            similarity += (100.0 * image_features[i].unsqueeze(0) @ text_features.T).softmax(dim=-1)
        similarity /= image_features.shape[0]

        top_probs, top_labels = similarity.cpu().topk(top_count, dim=-1)
        return [(text_array[top_labels[0][i].numpy()], (top_probs[0][i].numpy()*100)) for i in range(top_count)]

    def generate_caption(self, pil_image):
        gpu_image = transforms.Compose([
            transforms.Resize((blip_image_eval_size, blip_image_eval_size), interpolation=InterpolationMode.BICUBIC),
            transforms.ToTensor(),
            transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
        ])(pil_image).unsqueeze(0).type(self.dtype).to(devices.device_interrogate)

        with torch.no_grad():
            caption = self.blip_model.generate(gpu_image, sample=False, num_beams=shared.opts.interrogate_clip_num_beams, min_length=shared.opts.interrogate_clip_min_length, max_length=shared.opts.interrogate_clip_max_length)

        return caption[0]

    def interrogate(self, pil_image):
        res = None

        try:

            if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
                lowvram.send_everything_to_cpu()
                devices.torch_gc()

            self.load()

            caption = self.generate_caption(pil_image)
            self.send_blip_to_ram()
            devices.torch_gc()

            res = caption

            clip_image = self.clip_preprocess(pil_image).unsqueeze(0).type(self.dtype).to(devices.device_interrogate)

            with torch.no_grad(), devices.autocast():
                image_features = self.clip_model.encode_image(clip_image).type(self.dtype)

                image_features /= image_features.norm(dim=-1, keepdim=True)

                if shared.opts.interrogate_use_builtin_artists:
                    artist = self.rank(image_features, ["by " + artist.name for artist in shared.artist_db.artists])[0]

                    res += ", " + artist[0]

                for name, topn, items in self.categories:
                    matches = self.rank(image_features, items, top_count=topn)
                    for match, score in matches:
                        if shared.opts.interrogate_return_ranks:
                            res += f", ({match}:{score/100:.3f})"
                        else:
                            res += ", " + match

        except Exception:
            print(f"Error interrogating", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            res += "<error>"

        self.unload()

        return res
