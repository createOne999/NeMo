# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
# Copyright 2019 The Google Research Authors.
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


import torch

from nemo.core.classes import Dataset

__all__ = ['DialogueZeroShotIntentDataset']


class DialogueNearestNeighbourDataset(Dataset):
    """
    Dataset for training a NN model for zero shot intent recognition.
    """

    def __init__(self, dataset_split: str, dialogues_processor: object, tokenizer, cfg):
        """
        Args:
            dataset_split: dataset split
            dialogues_processor: Data generator for dialogues
            tokenizer: tokenizer to split text into sub-word tokens
        """
        self.cfg = cfg
        self.tokenizer = tokenizer
        self.raw_features = dialogues_processor.get_dialog_examples(dataset_split)
        self.examples = self._create_examples(self.raw_features)

    def _create_examples(self, raw_features):
        """Creates examples for the training and dev sets."""
        examples = []
        for idx in range(len(raw_features)):
            ex = self.raw_features[idx].data
            user_utterance = ex["utterance"]
            intent = ex["labels"]["intent"]
            sentences = [user_utterance]
            labels = [-1]
            for candidate_intent in ex["possible_labels"]["intent"]:
                text_b = "{} {}".format(self.cfg.prompt_template, candidate_intent)
                label = 1 if candidate_intent == intent else 0
                labels.append(label)
                sentences.append(text_b)
            encoded_input = self.tokenizer.tokenizer(
                sentences,
                padding='max_length',
                truncation=True,
                return_tensors='pt',
                max_length=self.cfg.max_seq_length,
            )
            examples.append((encoded_input, torch.tensor(labels)))
        return examples

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx: int):
        encoding, labels = self.examples[idx]
        input_ids = encoding['input_ids']
        attn_mask = encoding['attention_mask']
        return input_ids, attn_mask, labels
