# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import math
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytorch_lightning as L
import torch
import torch.distributed
import torch.nn.functional as F
from PIL import Image as PIL_Image
from megatron.core import dist_checkpointing
from megatron.core.optimizer import OptimizerConfig
from megatron.core.tensor_parallel.mappings import gather_from_tensor_model_parallel_region
from megatron.core.transformer.enums import ModelType
from megatron.core.transformer.transformer_config import TransformerConfig
from torch import nn, Tensor

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.llm import fn
from nemo.collections.llm.gpt.model import local_layer_spec, transformer_engine_layer_spec
from nemo.collections.llm.gpt.model.base import get_batch_on_this_context_parallel_rank, get_packed_seq_params
from nemo.collections.nlp.modules.common.megatron.module import MegatronModule
from nemo.collections.vlm.llama.image_transform import VariableSizeImageTransform
from nemo.collections.vlm.llama.model.transformer import (
    precompute_freqs_cis,
    _get_full_row_masked_out_mask, _stack_images, _pad_masks, VisionEncoder
)
from nemo.collections.vlm.llama.utils import get_negative_inf_value
from nemo.lightning import io, teardown
from nemo.lightning.megatron_parallel import MaskedTokenLossReductionWithLossMask
from nemo.lightning.pytorch.optim import MegatronOptimizerModule, OptimizerModule
from nemo.utils import logging

from megatron.core.transformer.mlp import MLPSubmodules
from megatron.core.tensor_parallel.layers import ColumnParallelLinear
from megatron.core.models.vision.multimodal_projector import MultimodalProjector


def llama_data_step(dataloader_iter) -> Dict[str, torch.Tensor]:
    from megatron.core import parallel_state

    # Based on: https://github.com/NVIDIA/Megatron-LM/blob/main/pretrain_gpt.py#L87
    # https://github.com/NVIDIA/NeMo/blob/main/nemo/collections/nlp/models/language_modeling/megatron_gpt_model.py#L828-L842

    batch = next(dataloader_iter)

    _batch: dict
    if isinstance(batch, tuple) and len(batch) == 3:
        _batch = batch[0]
    else:
        _batch = batch

    required_keys = set()
    required_keys.update(("attention_mask", "tokens",))
    if parallel_state.is_pipeline_first_stage():
        required_keys.update(("batch_images", "batch_masks", "total_len", "position_ids"))
    if parallel_state.is_pipeline_last_stage():
        required_keys.update(("labels", "loss_mask"))

    _batch = {
        key: val.cuda(non_blocking=True)
        if key in required_keys and isinstance(val, torch.Tensor) else val
        for key, val in _batch.items()
    }
    # slice batch along sequence dimension for context parallelism
    output = get_batch_on_this_context_parallel_rank(_batch)

    return output


def llama_forward_step(model, batch) -> torch.Tensor:
    forward_config = {
        "batch_images": batch["batch_images"],
        "batch_masks": batch["batch_masks"],
        "total_len": batch["total_len"],
        "tokens": batch.get("tokens", None),
        "position_ids": batch.get("position_ids", None),
    }

    if 'cu_seqlens' in batch:
        forward_config['packed_seq_params'] = get_packed_seq_params(batch)

    return model(**forward_config)


def set_input_tensor(self, tensor):
    pass


@dataclass
class CrossAttentionVisionModelConfig(TransformerConfig, io.IOMixin):
    # vision model params
    vision_chunk_size: int = -1  # image resolution for image models
    vision_max_num_chunks: int = 4
    num_global_layers: int = 8
    text_hidden_size: int = 4096
    gated: bool = False

    def configure_model(self) -> "CrossAttentionVisionModel":
        return CrossAttentionVisionModel(
            self,
        )

from nemo.collections.vlm.llama.model.language import CrossAttentionTextModel
from megatron.core.transformer.spec_utils import ModuleSpec
from nemo.lightning import get_vocab_size
from nemo.collections.llm.gpt.model.llama import Llama31Config, apply_rope_scaling

@dataclass
class CrossAttentionTextModelConfig(Llama31Config):
    num_cross_attention_layers: int = 8

    def _init_fusion_schedule(self, num_layers: int) -> List[int]:
        llama_layers = list(range(self.num_layers))
        # uniformly spread the layers
        k = math.ceil(len(llama_layers) / num_layers)
        return llama_layers[::-1][::k][:num_layers][::-1]

    def configure_model(self, tokenizer):
        self.fusion_schedule = self._init_fusion_schedule(self.num_cross_attention_layers)
        vp_size = self.virtual_pipeline_model_parallel_size
        if vp_size:
            p_size = self.pipeline_model_parallel_size
            assert (
                           self.num_layers // p_size
                   ) % vp_size == 0, "Make sure the number of model chunks is the same across all pipeline stages."

        from megatron.core import parallel_state

        transformer_layer_spec = self.transformer_layer_spec
        if not isinstance(transformer_layer_spec, ModuleSpec):
            transformer_layer_spec = transformer_layer_spec(self)

        if hasattr(self, 'vocab_size'):
            vocab_size = self.vocab_size
            logging.info(
                f"Use preset vocab_size: {vocab_size}, original vocab_size: {tokenizer.vocab_size}, dummy tokens:"
                f" {vocab_size - tokenizer.vocab_size}."
            )
        else:
            vocab_size = get_vocab_size(self, tokenizer.vocab_size, self.make_vocab_size_divisible_by)

        model = CrossAttentionTextModel(
            self,
            transformer_layer_spec=transformer_layer_spec,
            vocab_size=vocab_size,
            max_sequence_length=self.seq_length,
            fp16_lm_cross_entropy=self.fp16_lm_cross_entropy,
            parallel_output=self.parallel_output,
            share_embeddings_and_output_weights=self.share_embeddings_and_output_weights,
            position_embedding_type=self.position_embedding_type,
            rotary_percent=self.rotary_percent,
            rotary_base=self.rotary_base,
            seq_len_interpolation_factor=self.seq_len_interpolation_factor,
            pre_process=parallel_state.is_pipeline_first_stage(),
            post_process=parallel_state.is_pipeline_last_stage(),
        )
        model.rotary_pos_emb.inv_freq = apply_rope_scaling(
            model.rotary_pos_emb.inv_freq,
            factor=self.scale_factor,
            low_freq_factor=self.low_freq_factor,
            high_freq_factor=self.high_freq_factor,
            old_context_len=self.old_context_len,
        )
        return model

@dataclass
class CrossAttentionTextModelConfig8B(CrossAttentionTextModelConfig):
    rotary_base: int = 500_000
    seq_length: int = 8192
    num_layers: int = 32
    hidden_size: int = 4096
    ffn_hidden_size: int = 14336
    num_attention_heads: int = 32

@dataclass
class LlamaCrossAttentionModelConfig(TransformerConfig, io.IOMixin):
    language_model_config: Optional[TransformerConfig] = None
    vision_model_config: Optional[TransformerConfig] = None

    vision_num_cross_attention_layers: int = -1
    num_layers: int = 1  # Placeholder, NOT used!
    num_attention_heads: int = 8  # Placeholder, NOT used!

    language_model_from_pretrained: Optional[str] = None  # TODO
    vision_model_from_pretrained: Optional[str] = None  # TODO

    forward_step_fn: Callable = llama_forward_step
    data_step_fn: Callable = llama_data_step

    def __post_init__(self):
        model_config_attr = [
            'num_layers', 'hidden_size', 'num_attention_heads', 'num_query_groups',
            'ffn_hidden_size', 'kv_channels', 'hidden_dropout', 'attention_dropout',
            'fp32_residual_connection', 'apply_residual_connection_post_layernorm',
            'layernorm_epsilon', 'layernorm_zero_centered_gamma', 'add_bias_linear',
            'add_qkv_bias', 'gated_linear_unit', 'activation_func',
            'activation_func_fp8_input_store', 'num_moe_experts', 'rotary_interleaved',
            'window_size', 'normalization', 'qk_layernorm', 'test_mode',
            'calculate_per_token_loss'
        ]

        for attr in model_config_attr:
            setattr(self, attr, getattr(self.language_model_config, attr))

    def configure_model(self, tokenizer) -> "MCoreLlamaCrossAttentionModel":
        language_model = self.language_model_config.configure_model(tokenizer=tokenizer)
        vision_model = self.vision_model_config.configure_model()

        if self.language_model_from_pretrained is not None:
            sharded_state_dict = dict(state_dict=language_model.sharded_state_dict(prefix="module."))
            loaded_state_dict = dist_checkpointing.load(
                sharded_state_dict=sharded_state_dict, checkpoint_dir=self.language_model_from_pretrained
            )
            loaded_state_dict = {k.removeprefix("module."): v for k, v in loaded_state_dict["state_dict"].items()}
            language_model.load_state_dict(loaded_state_dict)
            logging.info(f"Restored language model weights from {self.language_model_from_pretrained}")

        model = MCoreLlamaCrossAttentionModel(
            config=self,
            language_model=language_model,
            vision_model=vision_model,
        )

        return model


class CrossAttentionVisionModel(MegatronModule):
    def __init__(self, config) -> None:
        super().__init__(config=config)
        return_intermediate = "3,7,15,23,30"
        self.vision_input_dim = 1280
        self.image_res = config.vision_chunk_size
        self.max_num_chunks = config.vision_max_num_chunks
        if return_intermediate is not None:
            return_intermediate = [int(l) for l in return_intermediate.split(",")]
            self.vision_input_dim = (
                                            len(return_intermediate) + 1
                                    ) * self.vision_input_dim
        self.patch_size = 14
        config.num_global_layers = 8
        self.vision_encoder = VisionEncoder(
            config=config,
            max_num_tiles=4,
            image_size=config.vision_chunk_size,
            patch_size=self.patch_size,
            return_intermediate=return_intermediate,
        )

        projection_config = copy.deepcopy(config)
        projection_config.hidden_size = config.text_hidden_size
        affine_layer_spec = MLPSubmodules(linear_fc1=ColumnParallelLinear, linear_fc2=None)
        self.vision_projection = MultimodalProjector(
            config=projection_config,
            submodules=affine_layer_spec,
            projector_type="affine",
            input_size=self.vision_input_dim,
        )

    def forward(
            self, images: torch.Tensor, aspect_ratios: torch.Tensor
    ) -> torch.Tensor:
        # vision_tokens: (B, T, D)
        # aspect_ratios: (B, T)
        # h: (B, T, D)
        vision_tokens = self.vision_encoder(
            images.to(dtype=torch.bfloat16), aspect_ratios
        )

        vision_tokens = self.vision_projection(vision_tokens)
        return vision_tokens

    def set_input_tensor(self, tensor):
        pass


class MCoreLlamaCrossAttentionModel(MegatronModule):
    def __init__(
            self,
            config: TransformerConfig,
            language_model: MegatronModule,
            vision_model: MegatronModule,
            pre_process: bool = True,
            post_process: bool = True,
    ) -> None:
        super().__init__(config=config)

        self.pre_process = pre_process
        self.post_process = post_process

        self.encoder_hidden_state = None
        self.vision_model = vision_model
        self.language_model = language_model
        self.model_type = ModelType.encoder_or_decoder

        self.image_res = config.vision_model_config.vision_chunk_size
        self.max_num_chunks = config.vision_model_config.vision_max_num_chunks
        self.image_transform = partial(
            VariableSizeImageTransform(size=self.image_res),
            max_num_chunks=self.max_num_chunks,
        )

    def setup_cache(self, max_batch_size: int, dtype: torch.dtype):
        self.language_model.setup_cache(max_batch_size, dtype)

    def compute_vision_tokens_masks(
            self,
            batch_images: List[List[PIL_Image.Image]],
            batch_masks: List[List[List[int]]],
            total_len: int,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        skip_vision_encoder = False

        assert len(batch_images) == len(
            batch_masks
        ), "Images and masks must have the same length"

        max_num_images = max(len(x) for x in batch_images)
        bsz = len(batch_images)

        if max_num_images == 0:
            num_chunks = [[self.max_num_chunks] for _ in batch_images]
            skip_vision_encoder = True
        else:
            images_and_aspect_ratios = [
                [self.image_transform(im) for im in row] for row in batch_images
            ]
            transformed_images = [
                [x[0] for x in row] for row in images_and_aspect_ratios
            ]

            aspect_ratios = torch.ones(bsz, max_num_images, 2, dtype=torch.int64)
            for i, row in enumerate(images_and_aspect_ratios):
                if len(row) > 0:
                    aspect_ratios[i, : len(row)] = torch.stack(
                        [torch.tensor(x[1]) for x in row]
                    )

            stacked_images, num_chunks = _stack_images(
                transformed_images,
                max_num_chunks=self.max_num_chunks,
                image_res=self.image_res,
                max_num_images=max_num_images,
            )

        if skip_vision_encoder:
            vision_tokens = torch.zeros(
                (
                    bsz,
                    max_num_images,
                    self.max_num_chunks,
                    int(
                        (self.vision_model.image_res / self.vision_model.patch_size)
                        ** 2
                        + 1
                    ),
                    self.model_dim,
                ), device="cuda"
            )
        else:
            stacked_images = stacked_images.cuda(non_blocking=True)
            aspect_ratios = aspect_ratios.cuda(non_blocking=True)
            vision_tokens = self.vision_model(stacked_images, aspect_ratios)

        bsz, nimg, nchunk, ntok, image_token_dim = tuple(vision_tokens.shape)

        xattn_caches = torch.stack(
            [
                layer.compute_xattn_kv_cache(
                    vision_tokens.view(bsz, -1, image_token_dim)
                )
                for layer in self.language_model.decoder.xattn_layers
            ]
        )
        padded_masks = _pad_masks(
            batch_masks,
            num_chunks,
            total_len,
            self.max_num_chunks,
        )

        cross_attention_masks, full_text_row_masked_out_mask = (
            self.language_model._get_xattn_mask(
                num_tokens=total_len,
                text_device="cuda",
                text_dtype=next(self.language_model.parameters()).dtype,
                vision_tokens=vision_tokens,
                cross_attention_masks=padded_masks,
            )
        )

        return (xattn_caches, cross_attention_masks, full_text_row_masked_out_mask)

    def set_input_tensor(self, tensor):
        pass

    def forward(
            self,
            position_ids: torch.Tensor,
            tokens: torch.Tensor,
            batch_images: Optional[List[List[PIL_Image.Image]]] = None,
            batch_masks: Optional[List[List[List[int]]]] = None,
            total_len: Optional[int] = None,
            cross_attention_masks: Optional[torch.Tensor] = None,
            full_text_row_masked_out_mask: Optional[torch.Tensor] = None,
            xattn_caches: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        if xattn_caches is None:
            xattn_caches, cross_attention_masks, full_text_row_masked_out_mask = (
                self.compute_vision_tokens_masks(
                    batch_images=batch_images,
                    batch_masks=batch_masks,
                    total_len=total_len,
                )
            )

        h = self.language_model.get_partially_trainable_embedding(tokens[:, position_ids])
        logits = self.language_model.forward(
            position_ids=position_ids,
            h=h,
            xattn_mask=cross_attention_masks[:, :, position_ids],
            full_text_row_masked_out_mask=full_text_row_masked_out_mask[
                                          :, :, position_ids
                                          ],
            xattn_caches=xattn_caches,
        )
        return logits


class LlamaCrossAttentionModel(L.LightningModule, io.IOMixin, io.ConnectorMixin, fn.FNMixin):
    def __init__(
            self,
            config: LlamaCrossAttentionModelConfig,
            optim: Optional[OptimizerModule] = None,
            tokenizer: Optional["TokenizerSpec"] = None,
            model_transform: Optional[Callable[[nn.Module], nn.Module]] = None,
    ):
        super().__init__()
        self.config = config
        self.tokenizer = tokenizer
        self.optim = optim or MegatronOptimizerModule(config=OptimizerConfig(lr=1e-4, use_distributed_optimizer=True))
        self.optim.connect(self)  # This will bind the `configure_optimizers` method
        self.model_transform = model_transform
        self._training_loss_reduction = None
        self._validation_loss_reduction = None

    def configure_model(self) -> None:
        if not hasattr(self, "module"):
            self.module = self.config.configure_model(self.tokenizer)

    def forward(
            self,
            batch_images: List[List[PIL_Image.Image]],
            batch_masks: List[List[List[int]]],
            total_len: int,
            tokens: torch.LongTensor,
            position_ids: torch.LongTensor,
            cross_attention_masks: Optional[torch.Tensor] = None,
            full_text_row_masked_out_mask: Optional[torch.Tensor] = None,
            xattn_caches: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:

        logits = self.module(
            position_ids=position_ids,
            tokens=tokens,
            batch_images=batch_images,
            batch_masks=batch_masks,
            total_len=total_len,
            cross_attention_masks=cross_attention_masks,
            full_text_row_masked_out_mask=full_text_row_masked_out_mask,
            xattn_caches=xattn_caches,
        )

        return logits

    def data_step(self, dataloader_iter) -> Dict[str, torch.Tensor]:
        return self.config.data_step_fn(dataloader_iter)

    def forward_step(self, batch) -> torch.Tensor:
        return self.config.forward_step_fn(self, batch)

    def training_step(self, batch, batch_idx=None) -> torch.Tensor:
        # In mcore the loss-function is part of the forward-pass (when labels are provided)
        return self.forward_step(batch)

    def validation_step(self, batch, batch_idx=None) -> torch.Tensor:
        # In mcore the loss-function is part of the forward-pass (when labels are provided)

        return self.forward_step(batch)

    @property
    def training_loss_reduction(self) -> MaskedTokenLossReductionWithLossMask:
        if not self._training_loss_reduction:
            self._training_loss_reduction = MaskedTokenLossReductionWithLossMask()

        return self._training_loss_reduction

    @property
    def validation_loss_reduction(self) -> MaskedTokenLossReductionWithLossMask:
        if not self._validation_loss_reduction:
            self._validation_loss_reduction = MaskedTokenLossReductionWithLossMask(validation_step=True)

        return self._validation_loss_reduction

@io.model_importer(LlamaCrossAttentionModel, "pytorch")
class PytorchLlamaCrossAttentionImporter(io.ModelConnector["LlamaCrossAttentionModel", LlamaCrossAttentionModel]):
    def init(self) -> LlamaCrossAttentionModel:
        return LlamaCrossAttentionModel(self.config, tokenizer=self.tokenizer)

    def apply(self, output_path: Path) -> Path:
        source = torch.load(str(self), map_location='cpu')
        class ModelState:
            def __init__(self, state_dict):
                self._state_dict = state_dict

            def state_dict(self):
                return self._state_dict

        source = ModelState(source)

        target = self.init()
        trainer = self.nemo_setup(target)

        self.convert_state(source, target)
        self.nemo_save(output_path, trainer)

        logging.info(f"Converted Llama Cross Attention model to Nemo, model saved to {output_path}")

        teardown(trainer, target)
        del trainer, target

        return output_path

    def convert_state(self, source, target):
        mapping = {
            "text_model.layers.*.feed_forward.mlp.layer_norm_weight": "decoder.layers.*.mlp.linear_fc1.layer_norm_weight",
            "text_model.layers.*.feed_forward.mlp.fc1_weight": "decoder.layers.*.mlp.linear_fc1.weight",
            "text_model.layers.*.feed_forward.mlp.fc2_weight": "decoder.layers.*.mlp.linear_fc2.weight",
            "text_model.layers.*.attention.wo.weight": "decoder.layers.*.self_attention.linear_proj.weight",
            "text_model.layers.*.attention.wqkv.layer_norm_weight": "decoder.layers.*.self_attention.linear_qkv.layer_norm_weight",
            "text_model.layers.*.attention.wqkv.weight": "decoder.layers.*.self_attention.linear_qkv.weight",
            "text_model.cross_attention_layers.*.attention.inner_attention.k_norm.weight": "decoder.xattn_layers.*.cross_attention.k_layernorm.weight",
            "text_model.cross_attention_layers.*.attention.wkv.weight": "decoder.xattn_layers.*.cross_attention.linear_kv.weight",
            "text_model.cross_attention_layers.*.attention.wo.weight": "decoder.xattn_layers.*.cross_attention.linear_proj.weight",
            "text_model.cross_attention_layers.*.attention.wq.layer_norm_weight": "decoder.xattn_layers.*.cross_attention.linear_q.layer_norm_weight",
            "text_model.cross_attention_layers.*.attention.wq.weight": "decoder.xattn_layers.*.cross_attention.linear_q.weight",
            "text_model.cross_attention_layers.*.attention.inner_attention.q_norm.weight": "decoder.xattn_layers.*.cross_attention.q_layernorm.weight",
            "text_model.cross_attention_layers.*.feed_forward.mlp.layer_norm_weight": "decoder.xattn_layers.*.mlp.linear_fc1.layer_norm_weight",
            "text_model.cross_attention_layers.*.feed_forward.mlp.fc1_weight": "decoder.xattn_layers.*.mlp.linear_fc1.weight",
            "text_model.cross_attention_layers.*.feed_forward.mlp.fc2_weight": "decoder.xattn_layers.*.mlp.linear_fc2.weight",
            "text_model.norm.weight": "decoder.final_layernorm.weight",
            "text_model.tok_embeddings.weight": "embedding.word_embeddings.weight",
            "text_model.learnable_embedding.weight": "learnable_embedding.weight",
            "text_model.output.weight": "output_layer.weight",
        }

        return io.apply_transforms(source, target, mapping=mapping, transforms=[_import_gate_attn, _import_gate_ffwd])

    @property
    def tokenizer(self) -> "AutoTokenizer":
        from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer
        # TODO: switch to using actual tokenizer of llama 3.2
        return AutoTokenizer(self.save_hf_tokenizer_assets("meta-llama/Meta-Llama-3.1-8B"))

    @property
    def config(self) -> CrossAttentionTextModelConfig:
        import json
        with open(self.parent / "params.json") as f:
            source = json.load(f)

        return CrossAttentionTextModelConfig(
            rotary_base=source['rope_theta'],
            seq_length=8192,
            num_layers=source['n_layers'],
            hidden_size=source['dim'],
            ffn_hidden_size=14336, #source['?']
            num_attention_heads=source['n_heads'],
        )

@io.state_transform(
    source_key="text_model.cross_attention_layers.*.gate_attn",
    target_key="decoder.xattn_layers.*.gate_attn",
)
def _import_gate_attn(gate_attn):
    return gate_attn[0:1]

@io.state_transform(
    source_key="text_model.cross_attention_layers.*.gate_ffwd",
    target_key="decoder.xattn_layers.*.gate_ffn",
)
def _import_gate_ffwd(gate_ffwd):
    return gate_ffwd[0:1]


__all__ = [
    "LlamaCrossAttentionModel",
    "LlamaCrossAttentionModelConfig",
    "llama_data_step",
    "llama_forward_step",
    "transformer_engine_layer_spec",
    "local_layer_spec",
]
