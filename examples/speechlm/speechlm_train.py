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

"""
Example usage:

export WANDB_API_KEY=${WANDB} && \
export CUDA_VISIBLE_DEVICES="1" && \
export HF_TOKEN=${HFTOKEN} && \
export HF_HOME="/home/heh/.huggingface/" && \
export HF_HUB_CACHE="/media/data/cache" && \
export NEMO_MODELS_CACHE="/media/data/pretrained_models/" && \
python salm_train.py \
    --config-path="/home/heh/github/NeMo-main/examples/speechlm/conf/"  \
    --config-name "salm_fc_linear" \
    data.train_ds.manifest_filepath=$TRAIN_MANIFESTS \
    data.validation_ds.manifest_filepath=$VAL_MANIFESTS \
    data.train_ds.num_workers=$NUM_WORKERS \
    data.validation_ds.num_workers=$NUM_WORKERS \
    ++data.validation_ds.name=$VAL_NAMES \
    data.common.global_batch_size=$GLOBAL_BATCH \
    data.common.micro_batch_size=$MICRO_BATCH \
    strategy.tensor_model_parallel_size=$TP \
    trainer.max_steps=1000000 \
    trainer.val_check_interval=20 \
    strategy.ckpt_async_save=false \  # This is important for `max_time_per_run` to work
    max_time_per_run="00:03:50:00"  # 3 hours 50 minutes, set to 'null' to disable
"""


from nemo.collections.speechlm.recipes import speech_to_text_llm_train
from nemo.core.config import hydra_runner


@hydra_runner(config_path="../conf", config_name="salm_fc_linear")
def main(cfg):
    return speech_to_text_llm_train(cfg)


if __name__ == "__main__":
    main()