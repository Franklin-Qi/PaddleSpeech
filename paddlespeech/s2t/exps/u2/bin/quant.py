# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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
"""Evaluation for U2 model."""
import os
import sys
from pathlib import Path

import paddle
import soundfile
from paddleslim import PTQ
from yacs.config import CfgNode

from paddlespeech.audio.transform.transformation import Transformation
from paddlespeech.s2t.frontend.featurizer.text_featurizer import TextFeaturizer
from paddlespeech.s2t.models.u2 import U2Model
from paddlespeech.s2t.training.cli import default_argument_parser
from paddlespeech.s2t.utils.log import Log
from paddlespeech.s2t.utils.utility import UpdateConfig
logger = Log(__name__).getlog()


class U2Infer():
    def __init__(self, config, args):
        self.args = args
        self.config = config
        self.audio_file = args.audio_file

        self.preprocess_conf = config.preprocess_config
        self.preprocess_args = {"train": False}
        self.preprocessing = Transformation(self.preprocess_conf)
        self.text_feature = TextFeaturizer(
            unit_type=config.unit_type,
            vocab=config.vocab_filepath,
            spm_model_prefix=config.spm_model_prefix)

        paddle.set_device('gpu' if self.args.ngpu > 0 else 'cpu')

        # model
        model_conf = config
        with UpdateConfig(model_conf):
            model_conf.input_dim = config.feat_dim
            model_conf.output_dim = self.text_feature.vocab_size
        model = U2Model.from_config(model_conf)
        self.model = model
        self.model.eval()
        self.ptq = PTQ()
        self.model = self.ptq.quantize(model)

        # load model
        params_path = self.args.checkpoint_path + ".pdparams"
        model_dict = paddle.load(params_path)
        self.model.set_state_dict(model_dict)

    def run(self):
        check(args.audio_file)

        with paddle.no_grad():
            # read
            audio, sample_rate = soundfile.read(
                self.audio_file, dtype="int16", always_2d=True)
            audio = audio[:, 0]
            logger.info(f"audio shape: {audio.shape}")

            # fbank
            feat = self.preprocessing(audio, **self.preprocess_args)
            logger.info(f"feat shape: {feat.shape}")

            ilen = paddle.to_tensor(feat.shape[0])
            xs = paddle.to_tensor(feat, dtype='float32').unsqueeze(0)
            decode_config = self.config.decode
            logger.info(f"decode cfg: {decode_config}")
            reverse_weight = getattr(decode_config, 'reverse_weight', 0.0)
            result_transcripts = self.model.decode(
                xs,
                ilen,
                text_feature=self.text_feature,
                decoding_method=decode_config.decoding_method,
                beam_size=decode_config.beam_size,
                ctc_weight=decode_config.ctc_weight,
                decoding_chunk_size=decode_config.decoding_chunk_size,
                num_decoding_left_chunks=decode_config.num_decoding_left_chunks,
                simulate_streaming=decode_config.simulate_streaming,
                reverse_weight=reverse_weight)
            rsl = result_transcripts[0][0]
            utt = Path(self.audio_file).name
            logger.info(f"hyp: {utt} {rsl}")
            # print(self.model)
            # print(self.model.forward_encoder_chunk)

            logger.info("-------------start quant ----------------------")
            batch_size = 1
            feat_dim = 80
            model_size = 512
            num_left_chunks = -1
            reverse_weight = 0.3
            logger.info(
                f"U2 Export Model Params: batch_size {batch_size}, feat_dim {feat_dim}, model_size {model_size}, num_left_chunks {num_left_chunks}, reverse_weight {reverse_weight}"
            )

            # ######################## self.model.forward_encoder_chunk ############
            # input_spec = [
            #     # (T,), int16
            #     paddle.static.InputSpec(shape=[None], dtype='int16'),
            # ]
            # self.model.forward_feature = paddle.jit.to_static(
            #     self.model.forward_feature, input_spec=input_spec)

            ######################### self.model.forward_encoder_chunk ############
            input_spec = [
                # xs, (B, T, D)
                paddle.static.InputSpec(
                    shape=[batch_size, None, feat_dim], dtype='float32'),
                # offset, int, but need be tensor
                paddle.static.InputSpec(shape=[1], dtype='int32'),
                # required_cache_size, int
                num_left_chunks,
                # att_cache
                paddle.static.InputSpec(
                    shape=[None, None, None, None], dtype='float32'),
                # cnn_cache
                paddle.static.InputSpec(
                    shape=[None, None, None, None], dtype='float32')
            ]
            self.model.forward_encoder_chunk = paddle.jit.to_static(
                self.model.forward_encoder_chunk, input_spec=input_spec)

            ######################### self.model.ctc_activation ########################
            input_spec = [
                # encoder_out, (B,T,D)
                paddle.static.InputSpec(
                    shape=[batch_size, None, model_size], dtype='float32')
            ]
            self.model.ctc_activation = paddle.jit.to_static(
                self.model.ctc_activation, input_spec=input_spec)

            ######################### self.model.forward_attention_decoder ########################
            input_spec = [
                # hyps, (B, U)
                paddle.static.InputSpec(shape=[None, None], dtype='int64'),
                # hyps_lens, (B,)
                paddle.static.InputSpec(shape=[None], dtype='int64'),
                # encoder_out, (B,T,D)
                paddle.static.InputSpec(
                    shape=[batch_size, None, model_size], dtype='float32'),
                reverse_weight
            ]
            self.model.forward_attention_decoder = paddle.jit.to_static(
                self.model.forward_attention_decoder, input_spec=input_spec)
            ################################################################################

            # jit save
            logger.info(f"export save: {self.args.export_path}")
            config = {
                'is_static': True,
                'combine_params': True,
                'skip_forward': True
            }
            self.ptq.save_quantized_model(self.model, self.args.export_path)
            # paddle.jit.save(
            #     self.model,
            #     self.args.export_path,
            #     combine_params=True,
            #     skip_forward=True)


def check(audio_file):
    if not os.path.isfile(audio_file):
        print("Please input the right audio file path")
        sys.exit(-1)

    logger.info("checking the audio file format......")
    try:
        sig, sample_rate = soundfile.read(audio_file)
    except Exception as e:
        logger.error(str(e))
        logger.error(
            "can not open the wav file, please check the audio file format")
        sys.exit(-1)
    logger.info("The sample rate is %d" % sample_rate)
    assert (sample_rate == 16000)
    logger.info("The audio file format is right")


def main(config, args):
    U2Infer(config, args).run()


if __name__ == "__main__":
    parser = default_argument_parser()
    # save asr result to
    parser.add_argument(
        "--result_file", type=str, help="path of save the asr result")
    parser.add_argument(
        "--audio_file", type=str, help="path of the input audio file")
    parser.add_argument(
        "--export_path",
        type=str,
        default='export',
        help="path of the input audio file")
    args = parser.parse_args()

    config = CfgNode(new_allowed=True)

    if args.config:
        config.merge_from_file(args.config)
    if args.decode_cfg:
        decode_confs = CfgNode(new_allowed=True)
        decode_confs.merge_from_file(args.decode_cfg)
        config.decode = decode_confs
    if args.opts:
        config.merge_from_list(args.opts)
    config.freeze()
    main(config, args)
