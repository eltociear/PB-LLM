import argparse
import copy

import torch
import torch.nn as nn
# from transformers import (
#     AutoModelForCausalLM,
#     AutoTokenizer,
#     DataCollatorForLanguageModeling,
#     TrainingArguments,
#     Trainer,
# )
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    TrainingArguments,
    Trainer,
    pipeline,
    AutoConfig,
)
from datasets import load_dataset
from quant import BinaryLinear, IrBinaryLinear, FdaBinaryLinear, XnorBinaryLinear
from utils import *
import torch.nn.functional as F
from evaluate import evaluate_model


def main(model_id, dataset_name):

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)

    # Load dataset
    data = load_dataset(args.dataset)
    data = data.map(lambda samples: tokenizer(samples["quote"]), batched=True)

    # model_fp16 = AutoModelForCausalLM.from_pretrained('facebook/opt-6.7b', device_map='auto')
    if args.load_checkpoint:
        model = AutoModelForCausalLM.from_pretrained(args.checkpoint_dir, device_map={"": 0})
    else:
        print('not loading checkpoint!!!')

    evaluate_model(model, tokenizer, args.model_id, args.tasks, limit=args.eval_limit, batch_size=args.eval_batch_size)

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Model Training Script")
    parser.add_argument(
        "--model_id", type=str, default="facebook/opt-350m", help="Pretrained model ID"
    )
    parser.add_argument(
        "--dataset", type=str, default="Abirate/english_quotes", help="Dataset name"
    )
    parser.add_argument(
        "--tasks",
        type=str,
        default="boolq",
        help="evaluate tasks name, can be tasks separated by , lambada_openai,piqa,arc_easy,arc_challenge,openbookqa, boolq",
    )
    parser.add_argument(
        "--eval_limit",
        default=-1,
        type=int,
        help="number of test samples for debug, set to -1 is no limit",
    )
    parser.add_argument(
        "--eval_batch_size",
        default=2,
        type=int,
        help="eval batch size, default is 2",
    )
    parser.add_argument(
        "--load_checkpoint",
        action="store_true",
        help="loading checkpoint or not"
    )
    parser.add_argument(
        "--checkpoint_dir", type=str, default='/data/shangyuzhang/BinaryLLM/checkpoints/', help="to-be-evaluated checkpoint dir"
    )
    args = parser.parse_args()

    main(args.model_id, args.dataset)