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

from typing import Any, Dict, List, Optional

import lightning.pytorch as pl
import numpy as np
import torch
from lightning.pytorch.callbacks import Callback

from nemo.lightning.pytorch.callbacks import PEFT
from nemo.utils import flops_formulas, logging

__all__ = ["FLOPsMeasurementCallback"]


class FLOPsMeasurementCallback(Callback):
    """
    Calculate FLOPs per second after last train step for a given job run.

    Args:
        model_config (Dict[str, Any]): params for running the experiment/job.
        Expects a nested dictionary with parent keys
            1. run- for assessing model name (Eg. 'gpt3', 'llama2', etc.) from sub-key 'name'.
                'name' usually has value like- train_gpt3_5b_*, which is matched to model name 'gpt3'.
            2. exp_manager- for accessing 'explicit_log_dir'. tensorboard log file is stored here,
                used for accessing step time needed for calculating TFLOPs per sec per GPU
            3. trainer- for accessing 'num_nodes' and 'devices' needed for calculating
                TFLOPs per sec per GPU
            4. model- Hyperparams for the model. Specifically- global batch size, sequence length,
                hidden size,  ffn hidden size, num_layers, num_attention_heads, num_query_groups,
                moe_router_topk. (list might increase with new models as required)
        log_dir (Optional[str]): Directory with tenbsorboard log file. If present, will overrride
            'explicit_log_dir' in model_config. Defaults to None.
        model_name (Optional[str]): If present, will override 'name' under 'run' in model_config.
            Defaults to None.
    """

    higher_is_better = True

    def __init__(
        self,
        model_config: Dict[str, Any],
        data_config: pl.LightningDataModule,
        model_name: Optional[str],
    ):
        self.model_cfg = model_config
        self.data_cfg = data_config

        # use config params only when NOT provided explicitly
        self.model = model_name

        self.gbs = self.data_cfg.global_batch_size
        self.enc_seq_len = self.model_cfg.seq_length
        self.hs = self.model_cfg.hidden_size
        self.layers = self.model_cfg.num_layers
        self.ffn_hs = self.model_cfg.ffn_hidden_size
        self.attention_heads = self.model_cfg.num_attention_heads
        self.moe_router_topk = self.model_cfg.moe_router_topk

        # this handles both- 1. key is present, value is None; 2. key is absent
        self.query_groups = self.model_cfg.num_query_groups
        if self.query_groups is None:
            self.query_groups = self.attention_heads

        self.model = self.model.lower() if self.model is not None else self.model

        self.avg_train_step_time = 0

    def on_train_start(self, trainer, pl_module):
        for callback in trainer.callbacks:
            if isinstance(callback, PEFT):
                raise NotImplementedError("FLOPs measurement not supported for finetuning jobs")

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx: int):
        """
        PyTorch Lightning callback hook to calculate TFLOPs per sec per GPU after training
        """
        try:
            self.avg_train_step_time += trainer.progress_bar_metrics['train_step_timing in s']
        except KeyError:
            print("'train_step_timing in s' not found. Make sure to use TimingCallback with FLOPsMeasurementCallback.")

        n = trainer.strategy.current_epoch_step
        if n % trainer.log_every_n_steps == 0:
            ## skip calculation if we haven't accumulated any timing data
            if self.avg_train_step_time == 0:
                return
            tflops_per_sec_per_gpu = self.eval_tflops_per_sec_per_gpu(
                self.avg_train_step_time / trainer.log_every_n_steps
            )
            self.avg_train_step_time = 0
            pl_module.log(
                "tflops_per_sec_per_gpu",
                tflops_per_sec_per_gpu,
                on_step=True,
                on_epoch=False,
                batch_size=1,
                prog_bar=True,
            )

    def eval_tflops_per_sec_per_gpu(self, train_step_time: List | float | int) -> float:
        """
        Args:
            train_step_time (Any[List, float, int]): Train step time (in seconds).
            Step time will be less stable for initial steps (~10 steps)- less
            accurate measurement
            Use average step time over several steps for higher accuracy
        Returns:
            (float): Model TFLOPs per sec per gpu
        """
        total_flops, flops_per_gpu = self.eval_model_flops()

        if not isinstance(train_step_time, list):
            train_step_time = [train_step_time]
        # efficient mean computation if num train steps is very large
        step_time_arr = np.array(train_step_time)
        train_step_time = np.mean(step_time_arr[len(step_time_arr) // 2 :])

        return flops_per_gpu / (1e12 * train_step_time)

    def eval_model_flops(self):
        """
        Calculate model FLOPs for a given model
        """

        model_flops_map = {
            "gpt3": flops_formulas.gpt3,
            "llama2": flops_formulas.llama2,
            "llama3": flops_formulas.llama3,
            "nemotron": flops_formulas.nemotron,
            "mixtral": flops_formulas.mixtral,
            "bert": flops_formulas.bert,
        }

        if self.model is not None:
            model_matches = [model for model in model_flops_map if model in self.model]
            self.model = model_matches[0] if len(model_matches) > 0 else self.model
        if self.model not in model_flops_map:
            logging.info(f"FLOPs measurement supported for {list(model_flops_map.keys())}")
            raise KeyError(f"Failed to extract valid model name from or missing FLOPs calculations for {self.model}")

        total_flops = model_flops_map[self.model]()
        num_devices = torch.distributed.get_world_size() if torch.distributed.is_initialized() else 1
        flops_per_gpu = total_flops / num_devices

        return total_flops, flops_per_gpu