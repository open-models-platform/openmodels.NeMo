# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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
from megatron.core import parallel_state
from omegaconf import OmegaConf
from omegaconf.omegaconf import open_dict
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.language_modeling.megatron_gpt_adapter_model import MegatronGPTAdapterLearningModel
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy
from nemo.core.config import hydra_runner

"""
This is the script to run an Adapter Tuned GPT Model for text generation.

Usage:
    Assume the model has TP=1, PP=1 in the following use cases.
    a. run greedy inference using a base gpt nemo file, and an adapter nemo file:
        python megatron_gpt_adapter_eval.py \
            gpt_model_file=PATH TO GPT MODEL NEMO FILE \
            adapter_model_file=PATH TO ADAPTER MODEL NEMO FILE (generated by training script: ./megatron_gpt_adapter_tuning.py) \
            data_paths=[PATH TO A JSONL FILE CONTAINING PROMPTS], \
            output_file=PATH TO OUTPUT FILE TO DUMP PREDICTIONS
"""

if not torch.cuda.is_available():
    raise EnvironmentError("GPU is needed for the inference")


@hydra_runner(config_path="conf", config_name="megatron_gpt_adapter_inference")
def main(cfg) -> None:

    # trainer required for restoring model parallel models
    trainer = Trainer(strategy=NLPDDPStrategy(), **cfg.trainer)

    # Load an adapter model,  must be provided in config
    if cfg.get("adapter_model_file", None) is not None:
        # Update frozen GPT model path in case it has changed
        adapter_tuning_cfg = MegatronGPTAdapterLearningModel.restore_from(
            cfg.adapter_model_file, trainer=trainer, return_config=True
        )
        with open_dict(adapter_tuning_cfg):
            adapter_tuning_cfg.language_model_path = cfg.gpt_model_file

        # Now load prompt learning model with frozen gpt model base
        model = MegatronGPTAdapterLearningModel.restore_from(
            restore_path=cfg.adapter_model_file, trainer=trainer, override_config_path=adapter_tuning_cfg
        )

    # Or load regular GPT model
    else:
        raise NotImplementedError(
            "This script is meant for inference from an Adapter Tuned GPT Model, for inference from a Megatron GPT model, refer to ../megatron_gpt_eval.py"
        )

    model.freeze()

    # Have to turn off activations_checkpoint_method for inference
    try:
        model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    try:
        model.frozen_model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    max_input_length = model.frozen_model.cfg.encoder_seq_length - cfg.inference.tokens_to_generate
    # check whether the DDP is initialized
    if parallel_state.is_unitialized():

        def dummy():
            return

        if trainer.strategy.launcher is not None:
            trainer.strategy.launcher.launch(dummy, trainer=trainer)
        trainer.strategy.setup_environment()

    _, dataloader = model.build_virtual_prompt_dataset(
        data=cfg.data_paths,
        batch_size=cfg.get("batch_size", 1),
        max_seq_length=max_input_length,
        min_seq_length=model.cfg.data.get('min_seq_length', 1),
        add_bos=cfg.inference.add_BOS,
        add_eos=False,
        for_train=False,
        tokens_to_generate=cfg.inference.tokens_to_generate,
        drop_last=False,
        shuffle=False,
    )

    config = OmegaConf.to_container(cfg.inference)
    model.set_inference_config(config)
    response = trainer.predict(model, dataloader)
    print("***************************")
    if cfg.output_file is not None:
        with open(cfg.output_file, "w", encoding="utf-8") as f:
            for batch in response:
                for sentence in batch['sentences']:
                    s = ' '.join(sentence.split('\n'))
                    f.write(s + "\n")
        print("predictions saved to {}".format(cfg.output_file))
    else:
        print(response)
    print("***************************")


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter
