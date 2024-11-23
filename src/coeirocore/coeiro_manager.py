import glob
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Iterable, Union, Dict

import librosa
import numpy as np
import pyworld as pw
import resampy
import sklearn.neighbors._partition_nodes
import sklearn.utils._typedefs
import torch
import yaml
from espnet2.bin.tts_inference import Text2Speech
from espnet2.text.phoneme_tokenizer import pyopenjtalk_g2p_prosody
from espnet2.text.token_id_converter import TokenIDConverter


@dataclass
class ModelPath:
    model_path: Path
    config_path: Path


class MetaManager:
    def __init__(self):
        self.speaker_infos: List[Dict] = []
        self.id_model_map: Dict[int, ModelPath] = {}

        speaker_paths: List[str] = sorted(glob.glob('./speaker_info/**/'))
        uuid_list: List = []
        for speaker_path in speaker_paths:
            with open(speaker_path + 'metas.json', encoding='utf-8') as f:
                meta = json.load(f)
            uuid = meta['speakerUuid']
            if uuid in uuid_list:
                raise Exception("SpeakerUuidの重複があります。")
            uuid_list.append(uuid)

            styles = []
            for style in meta['styles']:
                style_id = style['styleId']
                styles.append({'name': style['styleName'], 'id': style_id})

                if style_id in self.id_model_map.keys():
                    logging.warning("Style ids are duplicated")
                model_folder_path = f"{speaker_path}model/{style_id}/"
                self.id_model_map[style_id] = ModelPath(
                    model_path=Path(sorted(glob.glob(model_folder_path + '*.pth'))[0]),
                    config_path=Path(model_folder_path + 'config.yaml')
                )

            version = meta['version'] if 'version' in meta.keys() else '0.0.1'
            speaker_info = {
                'name': meta['speakerName'],
                'speaker_uuid': uuid,
                'styles': styles,
                'version': version
            }
            self.speaker_infos.append(speaker_info)
        self.speaker_infos = sorted(self.speaker_infos, key=lambda x: x['styles'][0]['id'])

    def get_metas_dict(self) -> List[dict]:
        return self.speaker_infos


class EspnetModel:
    def __init__(
            self,
            config_path: Path,
            model_path: Path,
            speed_scale=1.0,
            use_gpu=False
    ):
        device = 'cuda' if use_gpu else 'cpu'
        self.tts_model = Text2Speech(
            config_path,
            model_path,
            device=device,
            seed=0,
            # Only for FastSpeech & FastSpeech2 & VITS
            speed_control_alpha=speed_scale,
            # Only for VITS
            noise_scale=0.333,
            noise_scale_dur=0.333,
        )

        with open(config_path) as f:
            config = yaml.safe_load(f)
        self.token_id_converter = TokenIDConverter(
            token_list=config["token_list"],
            unk_symbol="<unk>",
        )

    @staticmethod
    def text2tokens(text: str) -> List[str]:
        return pyopenjtalk_g2p_prosody(text)

    def tokens2ids(
            self,
            tokens: Iterable[str]
    ) -> np.ndarray:
        return np.array(self.token_id_converter.tokens2ids(tokens), dtype=np.int64)

    def make_voice(
            self,
            text: Union[str, torch.Tensor, np.ndarray],
            seed: int = 0
    ) -> np.ndarray:
        np.random.seed(seed)
        torch.manual_seed(seed)
        output = self.tts_model(text)
        wav = output["wav"]
        wav = wav.view(-1).cpu().numpy()
        return wav


class AudioManager:
    def __init__(
            self,
            fs=44100,
            use_gpu=False
    ):
        self.fs = fs
        self.use_gpu = use_gpu

        self.meta_manager = MetaManager()
        self.previous_style_id = self.meta_manager.get_metas_dict()[0]['styles'][0]['id']
        self.previous_speed_scale = 1.0
        self.cache_speaker_models = {}

        self.cache_speaker_models[f"{self.previous_style_id}-{self.previous_speed_scale}"] = EspnetModel(
            model_path=self.meta_manager.id_model_map[self.previous_style_id].model_path,
            config_path=self.meta_manager.id_model_map[self.previous_style_id].config_path,
            speed_scale=self.previous_speed_scale,
            use_gpu=self.use_gpu
        )

    def synthesis(
            self,
            text: Union[str, List[str]],
            style_id: int,
            speed_scale: float = 1.0,
            volume_scale: float = 1.0,
            pitch_scale: float = 0,
            intonation_scale: float = 1.0,
            pre_phoneme_length: float = 0,
            post_phoneme_length: float = 0,
            output_sampling_rate: int = 44100
    ):
        # speaker_load
        if f"{style_id}-{speed_scale}" not in self.cache_speaker_models:
            self.cache_speaker_models[f"{style_id}-{speed_scale}"] = EspnetModel(
                model_path=self.meta_manager.id_model_map[style_id].model_path,
                config_path=self.meta_manager.id_model_map[style_id].config_path,
                speed_scale=1/speed_scale,
                use_gpu=self.use_gpu
            )
            self.previous_style_id = style_id
            self.previous_speed_scale = speed_scale

        current_speaker_model = self.cache_speaker_models[f"{style_id}-{speed_scale}"]

        # synthesis
        if not isinstance(text, str):
            text = current_speaker_model.tokens2ids(text)
        wav = current_speaker_model.make_voice(text)

        # post-processing
        wav = self.trim(wav)
        if volume_scale != 1:
            wav = self.volume(wav, volume_scale)
        if pitch_scale != 0 or intonation_scale != 1:
            wav = self.pitch_intonation(wav, self.fs, pitch_scale, intonation_scale)
        if pre_phoneme_length != 0 or post_phoneme_length != 0:
            wav = self.sil(wav, self.fs, pre_phoneme_length, post_phoneme_length)
        if output_sampling_rate != self.fs:
            wav = self.resampling(wav, self.fs, output_sampling_rate)

        return wav

    @staticmethod
    def trim(wav):
        return librosa.effects.trim(wav, top_db=30)[0]

    @staticmethod
    def volume(wav, volume_scale):
        return wav * volume_scale

    @staticmethod
    def pitch_intonation(wav, fs, pitch_scale, intonation_scale):
        f0, sp, ap = AudioManager.get_world(wav.astype(np.float64), fs)
        # pitch
        if pitch_scale != 0:
            f0 *= 2 ** pitch_scale
        # intonation
        if intonation_scale != 1:
            m = f0.mean()
            s = f0.std()
            f0_tmp = (f0 - m) / s
            f0 = (f0_tmp * (s * intonation_scale)) + m
        return pw.synthesize(f0, sp, ap, fs).astype(np.float32)

    @staticmethod
    def sil(wav, fs, pre_phoneme_length, post_phoneme_length):
        pre_pause = np.zeros(int(fs * pre_phoneme_length))
        post_pause = np.zeros(int(fs * post_phoneme_length))
        return np.concatenate([pre_pause, wav, post_pause], 0)

    @staticmethod
    def resampling(wav, fs, output_sampling_rate):
        return resampy.resample(
            wav,
            fs,
            output_sampling_rate,
            filter="kaiser_fast",
        )

    # https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder/blob/3a7c99a32c717deb8e66bde64b5e60b1a4afce79/demo/demo.py
    @staticmethod
    def get_world(x, fs):
        _f0_h, t_h = pw.harvest(x, fs)
        f0_h = pw.stonemask(x, _f0_h, t_h, fs)
        sp_h = pw.cheaptrick(x, f0_h, t_h, fs)
        ap_h = pw.d4c(x, f0_h, t_h, fs)
        return f0_h, sp_h, ap_h
