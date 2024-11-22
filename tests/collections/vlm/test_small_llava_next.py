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

## NOTE: This script is present for github-actions testing only.
## There are no guarantees that this script is up-to-date with latest NeMo.

import argparse

import torch
from megatron.core.optimizer import OptimizerConfig
from pytorch_lightning.loggers import TensorBoardLogger
from transformers import AutoProcessor

from nemo import lightning as nl
from nemo.collections import llm, vlm
from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer
from nemo.collections.llm.api import train
from nemo.lightning import AutoResume, NeMoLogger
from nemo.lightning.pytorch.callbacks import ModelCheckpoint, ParameterDebugger
from nemo.lightning.pytorch.optim.megatron import MegatronOptimizerModule


def get_args():
    parser = argparse.ArgumentParser(description='Train a small Llava Next model using NeMo 2.0')
    parser.add_argument('--devices', type=int, default=1, help="Number of devices to use for training")
    parser.add_argument('--max-steps', type=int, default=5, help="Number of steps to train for")
    parser.add_argument(
        '--experiment-dir', type=str, default=None, help="directory to write results and checkpoints to"
    )

    return parser.parse_args()


if __name__ == '__main__':

    args = get_args()

    gbs = 2
    mbs = 1
    seq_length = 576
    decoder_seq_length = 1024
    processor = AutoProcessor.from_pretrained("llava-hf/llava-v1.6-vicuna-7b-hf")
    tokenizer = AutoTokenizer("llava-hf/llava-v1.6-vicuna-7b-hf")

    data = vlm.NevaMockDataModule(
        seq_length=decoder_seq_length,
        tokenizer=tokenizer,
        image_processor=processor.image_processor,
        is_llava_next=True,
        global_batch_size=gbs,
        micro_batch_size=mbs,
        num_workers=0,
    )

    # Transformer configurations
    language_transformer_config = llm.Llama2Config7B(seq_length=decoder_seq_length, num_layers=2)

    vision_transformer_config = vlm.HFCLIPVisionConfig(
        pretrained_model_name_or_path="openai/clip-vit-large-patch14-336"
    )
    vision_projection_config = vlm.MultimodalProjectorConfig(
        projector_type="mlp2x_gelu",
        input_size=1024,
        hidden_size=4096,
        ffn_hidden_size=4096,
    )

    # NEVA model configuration
    neva_config = vlm.NevaConfig(
        language_transformer_config=language_transformer_config,
        vision_transformer_config=vision_transformer_config,
        vision_projection_config=vision_projection_config,
        freeze_language_model=True,
        freeze_vision_model=True,
        is_llava_next=True,
    )

    model = vlm.NevaModel(neva_config, tokenizer=data.tokenizer)

    strategy = nl.MegatronStrategy(
        tensor_model_parallel_size=1,
        pipeline_model_parallel_size=1,
        encoder_pipeline_model_parallel_size=0,
        pipeline_dtype=torch.bfloat16,
    )
    checkpoint_callback = ModelCheckpoint(
        every_n_train_steps=5000,
        save_optim_on_train_end=True,
    )

    def create_verify_precision(precision: torch.dtype):
        def verify_precision(tensor: torch.Tensor) -> None:
            assert tensor.dtype == precision

        return verify_precision

    debugger = ParameterDebugger(
        param_fn=create_verify_precision(torch.bfloat16),
        grad_fn=create_verify_precision(torch.float32),
        log_on_hooks=["on_train_start", "on_train_end"],
    )
    callbacks = [checkpoint_callback, debugger]

    loggers = []
    tensorboard_logger = TensorBoardLogger(
        save_dir='dummy',  ## NOTE: this gets overwritten by default
    )
    loggers.append(tensorboard_logger)

    opt_config = OptimizerConfig(
        optimizer='adam',
        lr=6e-4,
        min_lr=6e-5,
        use_distributed_optimizer=False,
        bf16=True,
    )
    opt = MegatronOptimizerModule(config=opt_config)

    trainer = nl.Trainer(
        devices=args.devices,
        max_steps=args.max_steps,
        accelerator="gpu",
        strategy=strategy,
        logger=loggers,
        callbacks=callbacks,
        log_every_n_steps=1,
        limit_val_batches=2,
        plugins=nl.MegatronMixedPrecision(precision="bf16-mixed"),
    )

    nemo_logger = NeMoLogger(
        log_dir=args.experiment_dir,
    )

    resume = AutoResume(
        resume_if_exists=True,
        resume_ignore_no_checkpoint=True,
    )

    train(
        model=model,
        data=data,
        trainer=trainer,
        log=nemo_logger,
        resume=resume,
        tokenizer='data',
        optim=opt,
    )