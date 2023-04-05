# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
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

import os
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import List, Optional

import torch
from omegaconf import OmegaConf
from utils.data_prep import (
    add_t_start_end_to_utt_obj,
    get_batch_starts_ends,
    get_batch_variables,
    get_manifest_lines_batch,
    is_entry_in_all_lines,
    is_entry_in_any_lines,
)
from utils.make_ass_files import make_ass_files
from utils.make_ctm_files import make_ctm_files
from utils.make_output_manifest import write_manifest_out_line
from utils.viterbi_decoding import viterbi_decoding

from nemo.collections.asr.models.ctc_models import EncDecCTCModel
from nemo.collections.asr.parts.utils.transcribe_utils import setup_model
from nemo.core.config import hydra_runner
from nemo.utils import logging


"""
Align the utterances in manifest_filepath. 
Results are saved in ctm files in output_dir.

Arguments:
    pretrained_name: string specifying the name of a CTC NeMo ASR model which will be automatically downloaded
        from NGC and used for generating the log-probs which we will use to do alignment.
        Note: NFA can only use CTC models (not Transducer models) at the moment.
    model_path: string specifying the local filepath to a CTC NeMo ASR model which will be used to generate the
        log-probs which we will use to do alignment.
        Note: NFA can only use CTC models (not Transducer models) at the moment.
        Note: if a model_path is provided, it will override the pretrained_name.
    manifest_filepath: filepath to the manifest of the data you want to align,
        containing 'audio_filepath' and 'text' fields.
    output_dir: the folder where output CTM files and new JSON manifest will be saved.
    align_using_pred_text: if True, will transcribe the audio using the specified model and then use that transcription 
        as the 'ground truth' for the forced alignment. 
    transcribe_device: None, or a string specifying the device that will be used for generating log-probs (i.e. "transcribing").
        The string needs to be in a format recognized by torch.device(). If None, NFA will set it to 'cuda' if it is available 
        (otherwise will set it to 'cpu').
    viterbi_device: None, or string specifying the device that will be used for doing Viterbi decoding. 
        The string needs to be in a format recognized by torch.device(). If None, NFA will set it to 'cuda' if it is available 
        (otherwise will set it to 'cpu').
    batch_size: int specifying batch size that will be used for generating log-probs and doing Viterbi decoding.
    TODO: update description and variable name:
    additional_ctm_grouping_separator:  the string used to separate CTM segments if you want to obtain CTM files at a 
        level that is not the token level or the word level. NFA will always produce token-level and word-level CTM 
        files in: `<output_dir>/tokens/<utt_id>.ctm` and `<output_dir>/words/<utt_id>.ctm`. 
        If `additional_ctm_grouping_separator` is specified, an additional folder 
        `<output_dir>/{tokens/words/additional_segments}/<utt_id>.ctm` will be created containing CTMs 
        for `addtional_ctm_grouping_separator`-separated segments. 
    remove_blank_tokens_from_ctm:  a boolean denoting whether to remove <blank> tokens from token-level output CTMs. 
    audio_filepath_parts_in_utt_id: int specifying how many of the 'parts' of the audio_filepath
        we will use (starting from the final part of the audio_filepath) to determine the 
        utt_id that will be used in the CTM files. Note also that any spaces that are present in the audio_filepath 
        will be replaced with dashes, so as not to change the number of space-separated elements in the 
        CTM files.
        e.g. if audio_filepath is "/a/b/c/d/e 1.wav" and audio_filepath_parts_in_utt_id is 1 => utt_id will be "e1"
        e.g. if audio_filepath is "/a/b/c/d/e 1.wav" and audio_filepath_parts_in_utt_id is 2 => utt_id will be "d_e1"
        e.g. if audio_filepath is "/a/b/c/d/e 1.wav" and audio_filepath_parts_in_utt_id is 3 => utt_id will be "c_d_e1"
    minimum_timestamp_duration: a float indicating a minimum duration (in seconds) for timestamps in the CTM. If any 
        line in the CTM has a duration lower than the `minimum_timestamp_duration`, it will be enlarged from the 
        middle outwards until it meets the minimum_timestamp_duration, or reaches the beginning or end of the audio 
        file. Note that this may cause timestamps to overlap.
"""


@dataclass
class CTMFileConfig:
    remove_blank_tokens: bool = False


@dataclass
class ASSFileConfig:
    fontsize: int = 20
    marginv: int = 20


@dataclass
class AlignmentConfig:
    # Required configs
    pretrained_name: Optional[str] = None
    model_path: Optional[str] = None
    manifest_filepath: Optional[str] = None
    output_dir: Optional[str] = None

    # General configs
    align_using_pred_text: bool = False
    transcribe_device: Optional[str] = None
    viterbi_device: Optional[str] = None
    batch_size: int = 1
    additional_ctm_grouping_separator: Optional[str] = None
    minimum_timestamp_duration: float = 0
    audio_filepath_parts_in_utt_id: int = 1

    save_output_file_formats: List[str] = field(default_factory=lambda: ["ctm", "ass"])
    ctm_file_config: CTMFileConfig = CTMFileConfig()
    ass_file_config: ASSFileConfig = ASSFileConfig()


@hydra_runner(config_name="AlignmentConfig", schema=AlignmentConfig)
def main(cfg: AlignmentConfig):

    logging.info(f'Hydra config: {OmegaConf.to_yaml(cfg)}')

    if is_dataclass(cfg):
        cfg = OmegaConf.structured(cfg)

    # Validate config
    if cfg.model_path is None and cfg.pretrained_name is None:
        raise ValueError("Both cfg.model_path and cfg.pretrained_name cannot be None")

    if cfg.model_path is not None and cfg.pretrained_name is not None:
        raise ValueError("One of cfg.model_path and cfg.pretrained_name must be None")

    if cfg.manifest_filepath is None:
        raise ValueError("cfg.manifest_filepath must be specified")

    if cfg.output_dir is None:
        raise ValueError("cfg.output_dir must be specified")

    if cfg.batch_size < 1:
        raise ValueError("cfg.batch_size cannot be zero or a negative number")

    if cfg.additional_ctm_grouping_separator == "" or cfg.additional_ctm_grouping_separator == " ":
        raise ValueError("cfg.additional_grouping_separator cannot be empty string or space character")

    if cfg.minimum_timestamp_duration < 0:
        raise ValueError("cfg.minimum_timestamp_duration cannot be a negative number")

    # Validate manifest contents
    if not is_entry_in_all_lines(cfg.manifest_filepath, "audio_filepath"):
        raise RuntimeError(
            "At least one line in cfg.manifest_filepath does not contain an 'audio_filepath' entry. "
            "All lines must contain an 'audio_filepath' entry."
        )

    if cfg.align_using_pred_text:
        if is_entry_in_any_lines(cfg.manifest_filepath, "pred_text"):
            raise RuntimeError(
                "Cannot specify cfg.align_using_pred_text=True when the manifest at cfg.manifest_filepath "
                "contains 'pred_text' entries. This is because the audio will be transcribed and may produce "
                "a different 'pred_text'. This may cause confusion."
            )
    else:
        if not is_entry_in_all_lines(cfg.manifest_filepath, "text"):
            raise RuntimeError(
                "At least one line in cfg.manifest_filepath does not contain a 'text' entry. "
                "NFA requires all lines to contain a 'text' entry when cfg.align_using_pred_text=False."
            )

    # init devices
    if cfg.transcribe_device is None:
        transcribe_device = torch.device("cuda" if torch.cuda.is_available else "cpu")
    else:
        transcribe_device = torch.device(cfg.transcribe_device)
    logging.info(f"Device to be used for transcription step (`transcribe_device`) is {transcribe_device}")

    if cfg.viterbi_device is None:
        viterbi_device = torch.device("cuda" if torch.cuda.is_available else "cpu")
    else:
        viterbi_device = torch.device(cfg.viterbi_device)
    logging.info(f"Device to be used for viterbi step (`viterbi_device`) is {viterbi_device}")

    if transcribe_device.type == 'cuda' or viterbi_device.type == 'cuda':
        logging.warning(
            'One or both of transcribe_device and viterbi_device are GPUs. If you run into OOM errors '
            'it may help to change both devices to be the CPU.'
        )

    # load model
    model, _ = setup_model(cfg, transcribe_device)
    model.eval()

    if not isinstance(model, EncDecCTCModel):
        raise NotImplementedError(
            f"Model {cfg.model_name} is not an instance of NeMo EncDecCTCModel."
            " Currently only instances of EncDecCTCModels are supported"
        )

    if cfg.minimum_timestamp_duration > 0:
        logging.warning(
            f"cfg.minimum_timestamp_duration has been set to {cfg.minimum_timestamp_duration} seconds. "
            "This may cause the alignments for some tokens/words/additional segments to be overlapping."
        )

    # get start and end line IDs of batches
    starts, ends = get_batch_starts_ends(cfg.manifest_filepath, cfg.batch_size)

    if cfg.align_using_pred_text:
        # record pred_texts to save them in the new manifest at the end of this script
        pred_text_all_lines = []
    else:
        pred_text_all_lines = None

    # init output_timestep_duration = None and we will calculate and update it during the first batch
    output_timestep_duration = None

    # init f_manifest_out
    os.makedirs(cfg.output_dir, exist_ok=True)
    tgt_manifest_name = str(Path(cfg.manifest_filepath).stem) + "_with_ctm_paths.json"
    tgt_manifest_filepath = str(Path(cfg.output_dir) / tgt_manifest_name)
    f_manifest_out = open(tgt_manifest_filepath, 'w')

    # get alignment and save in CTM batch-by-batch
    for start, end in zip(starts, ends):
        manifest_lines_batch = get_manifest_lines_batch(cfg.manifest_filepath, start, end)

        (log_probs_batch, y_batch, T_batch, U_batch, utt_obj_batch, output_timestep_duration,) = get_batch_variables(
            manifest_lines_batch,
            model,
            cfg.additional_ctm_grouping_separator,
            cfg.align_using_pred_text,
            cfg.audio_filepath_parts_in_utt_id,
            output_timestep_duration,
        )

        alignments_batch = viterbi_decoding(log_probs_batch, y_batch, T_batch, U_batch, viterbi_device)

        for utt_obj, alignment_utt in zip(utt_obj_batch, alignments_batch):

            utt_obj = add_t_start_end_to_utt_obj(utt_obj, alignment_utt, output_timestep_duration)

            if "ctm" in cfg.save_output_file_formats:
                utt_obj = make_ctm_files(
                    utt_obj, model, cfg.output_dir, cfg.minimum_timestamp_duration, cfg.ctm_file_config,
                )

            if "ass" in cfg.save_output_file_formats:
                make_ass_files(
                    utt_obj, model, cfg.output_dir, cfg.minimum_timestamp_duration, cfg.ass_file_config,
                )

            write_manifest_out_line(
                f_manifest_out, utt_obj,
            )

    f_manifest_out.close()

    return None


if __name__ == "__main__":
    main()
