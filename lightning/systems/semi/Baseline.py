import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from text.define import LANG_NAME2ID
from lightning.build import build_all_speakers, build_id2symbols
from lightning.systems.system import System
from lightning.model import FastSpeech2Loss, FastSpeech2
from lightning.callbacks.language.baseline_saver import Saver
from lightning.model.embeddings import MultilingualEmbedding
from lightning.model.codebook import SoftMultiAttCodebook
from lightning.model.text_encoder import TextEncoder


class BaselineSystem(System):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def build_configs(self):
        self.spk_config = {
            "emb_type": self.model_config["speaker_emb"],
            "speakers": build_all_speakers(self.data_configs)
        }
        self.bs = self.train_config["optimizer"]["batch_size"]
    
    def build_model(self):
        self.use_matching = self.model_config["use_matching"]
        encoder_dim = self.model_config["transformer"]["encoder_hidden"]
        self.embedding_model = MultilingualEmbedding(
            id2symbols=build_id2symbols(self.data_configs), dim=encoder_dim)
        self.text_encoder = TextEncoder(self.model_config["text_encoder"])
        self.model = FastSpeech2(self.model_config, spk_config=self.spk_config)
        self.loss_func = FastSpeech2Loss(self.model_config)

        if self.use_matching:
            self.shared_emb_banks = nn.Parameter(
                torch.randn(self.model_config["matching"]["codebook_size"], self.model_config["matching"]["dim"]))
            self.text_matching = SoftMultiAttCodebook(
                codebook_size=self.model_config["matching"]["codebook_size"],
                embed_dim=self.model_config["matching"]["dim"],
                num_heads=self.model_config["matching"]["nhead"],
            )
            self.text_matching.emb_banks = self.shared_emb_banks
      
    def build_optimized_model(self):
        opt_modules = [self.text_encoder, self.model, self.embedding_model]
        if self.use_matching:
            opt_modules += [self.text_matching]
        return nn.ModuleList(opt_modules)
    
    def build_saver(self):
        self.saver = Saver(self.data_configs, self.model_config, self.log_dir, self.result_dir)
        return self.saver

    def common_step(self, batch, batch_idx, train=True):
        emb_texts = self.embedding_model(batch[3])
        emb_texts = self.text_encoder(emb_texts, lengths=batch[4])
        if self.use_matching:
            emb_texts, _ = self.text_matching(emb_texts)
        output = self.model(batch[2], emb_texts, *(batch[4:]))
        loss = self.loss_func(batch[:-1], output)
        loss_dict = {
            "Total Loss"       : loss[0],
            "Mel Loss"         : loss[1],
            "Mel-Postnet Loss" : loss[2],
            "Pitch Loss"       : loss[3],
            "Energy Loss"      : loss[4],
            "Duration Loss"    : loss[5],
        }
        return loss_dict, output
    
    def synth_step(self, batch, batch_idx):
        emb_texts = self.embedding_model(batch[3])
        emb_texts = self.text_encoder(emb_texts, lengths=batch[4])
        if self.use_matching:
            emb_texts, _ = self.text_matching(emb_texts)
        output = self.model(batch[2], emb_texts, *(batch[4:6]), lang_args=batch[-1], average_spk_emb=True)
        return output
    
    def on_train_batch_start(self, batch, batch_idx, dataloader_idx):
        assert len(batch) == 13, f"data with 13 elements, but get {len(batch)}"
    
    def on_validation_batch_start(self, batch, batch_idx, dataloader_idx):
        assert len(batch) == 13, f"data with 13 elements, but get {len(batch)}"
    
    def training_step(self, batch, batch_idx):
        train_loss_dict, output = self.common_step(batch, batch_idx, train=True)

        # Log metrics to CometLogger
        loss_dict = {f"Train/{k}": v.item() for k, v in train_loss_dict.items()}
        self.log_dict(loss_dict, sync_dist=True, batch_size=self.bs)
        return {'loss': train_loss_dict["Total Loss"], 'losses': train_loss_dict, 'output': output, '_batch': batch}

    def validation_step(self, batch, batch_idx):
        val_loss_dict, predictions = self.common_step(batch, batch_idx, train=False)
        synth_predictions = self.synth_step(batch, batch_idx)

        # Log metrics to CometLogger
        loss_dict = {f"Val/{k}": v.item() for k, v in val_loss_dict.items()}
        self.log_dict(loss_dict, sync_dist=True, batch_size=self.bs)
        return {'loss': val_loss_dict["Total Loss"], 'losses': val_loss_dict, 'output': predictions, '_batch': batch, 'synth': synth_predictions}

    def inference(self, spk_ref_mel_slice: np.ndarray, text: np.ndarray, symbol_id: str, lang_id: str=None):
        """
        Return FastSpeech2 results:
            (
                output,
                postnet_output,
                p_predictions,
                e_predictions,
                log_d_predictions,
                d_rounded,
                src_masks,
                mel_masks,
                src_lens,
                mel_lens,
            )
        """
        spk_args = (torch.from_numpy(spk_ref_mel_slice).to(self.device), [slice(0, spk_ref_mel_slice.shape[0])])
        if lang_id is not None:
            lang_args = torch.LongTensor([LANG_NAME2ID[lang_id]]).to(self.device)
        else:
            lang_args = None
        texts = torch.from_numpy(text).long().unsqueeze(0).to(self.device)
        src_lens = torch.LongTensor([len(text)]).to(self.device)
        max_src_len = max(src_lens)
        
        with torch.no_grad():
            emb_texts = self.embedding_model(texts, symbol_id)
            emb_texts = self.text_encoder(emb_texts, lengths=src_lens)
            if self.use_matching:
                emb_texts, _ = self.text_matching(emb_texts)
            output = self.model(spk_args, emb_texts, src_lens, max_src_len, lang_args=lang_args, average_spk_emb=True)

        return output
