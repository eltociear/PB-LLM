import sys

sys.path.append(".")
import argparse
import os
import torch
import torch.nn as nn
import copy 
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
    default_data_collator, 
    TrainingArguments,
    Trainer,
    LlamaTokenizer,
    LlamaForCausalLM,
)
from transformers.models.opt.modeling_opt import OPTForCausalLM

# from transformers import LlamaTokenizer, LlamaForCausalLM
from datasets import load_dataset
from datautils import get_qat_dataset
from quant import (
    XnorBinaryLinear,
    BinaryLinear,
    IrBinaryLinear,
    BiRealLinear,
    OutliersQLinearColumn,
    BinaryXnorExceptOutliersLinear,
    LowbitQuantizeLinear,
)

# , FdaBinaryLinear
from utils import (
    print_trainable_parameters,
    print_memory_usage,
    prepare_model_for_training,
    save_bnn,
)
from evaluate import evaluate_model

"""
Usage
python bnn_train_layerwise.py --binarization_method xnor --debug
CUDA_VISIBLE_DEVICES='4,5' XDG_CACHE_HOME='/data/shangyuzhang/' python experiments/column_quant.py --binarization_method=xnor_outlier --model_save_dir "./checkpoints/openllama-3b-9-oldversion" --granularity=whole_model --model_id=openlm-research/open_llama_3b --train_step=2000 --dataset=red_pajama
"""

from transformers import get_cosine_with_hard_restarts_schedule_with_warmup

def get_scheduler(num_training_steps: int):
    def lr_scheduler(optimizer):
        return get_cosine_with_hard_restarts_schedule_with_warmup(optimizer, 
                                                                  num_warmup_steps=100, 
                                                                  num_training_steps=num_training_steps,
                                                                  num_cycles=5)
    return lr_scheduler


def replace_qlinear(root_module, name_prefix=""):
    module_name_dict = {name: module for name, module in root_module.named_modules()}
    for name, module in module_name_dict.items():
        if isinstance(module, nn.Linear):
            print(name)
            ind = name.rfind(".")
            if ind == -1:
                father = module_name_dict[""]
            else:
                father = module_name_dict[name[:ind]]
            # choose binariztaion method
            if "xnor" in args.binarization_method:
                quant_cls = XnorBinaryLinear
            elif "ste" in args.binarization_method:
                quant_cls = BinaryLinear
            elif "ir" in args.binarization_method:
                quant_cls = IrBinaryLinear
            elif "fda" in args.binarization_method:
                quant_cls = FdaBinaryLinear
            elif "bireal" in args.binarization_method:
                quant_cls = BiRealLinear
            elif 'lowbit_quant' in args.binarization_method:
                quant_cls = LowbitQuantizeLinear
            else:
                print(f"not supported binarization method {args.binarization_method}")
                raise NotImplementedError
            if "act_outlier_column" in args.binarization_method:
                qlinear = OutliersQLinearColumn(
                    module.weight,
                    module.bias,
                    dense_class=quant_cls,
                    outlier_metric="act_L1",
                    outlier_fraction=args.outlier_fraction,
                )
            elif "outlier_column" in args.binarization_method:
                qlinear = OutliersQLinearColumn(
                    module.weight,
                    module.bias,
                    dense_class=quant_cls,
                    outlier_fraction=args.outlier_fraction,
                )
            # elif "outlier" in args.binarization_method:
            #     qlinear = OutliersLinear(
            #         module.weight, module.bias, dense_class=quant_cls
            #     )
            elif "outlier" in args.binarization_method:
                qlinear = BinaryXnorExceptOutliersLinear(module.weight, module.bias)
            elif 'lowbit_quant' in args.binarization_method:
                qlinear = LowbitQuantizeLinear(module.weight, None, w_bits=4)
            else:
                qlinear = quant_cls(module.weight, module.bias)

            setattr(father, name[ind + 1 :], qlinear)
            print(f"replace layer {name_prefix}{name} with {qlinear}")


def iterative_train(model, ordered_name_modules, data, tokenizer):
    """
    ordered_name_modules: [(name, module), ...]
    """
    for module_name, module in ordered_name_modules:
        print_trainable_parameters(model)
        replace_qlinear(module, f"{module_name}.")

        # Define training arguments
        if args.train_steps:
            training_args = TrainingArguments(
                per_device_train_batch_size=1,
                gradient_accumulation_steps=1,
                warmup_steps=args.train_steps*0.05,
                max_steps=args.train_steps,
                learning_rate=1e-4,
                lr_scheduler_type="cosine",
                fp16=False,
                logging_steps=1,
                output_dir="outputs",
                optim="adamw_torch",
                report_to="tensorboard",
            )

            # Create trainer
            trainer = Trainer(
                model=model,
                train_dataset=data,
                args=training_args,
                data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
                # data_collator=default_data_collator,
                # lr_scheduler=get_scheduler(args.train_steps)
            )

            model.config.use_cache = (
                False  # silence the warnings. Please re-enable for inference!
            )

            # Train the model
            trainer.train()
        for name, param in model.named_parameters():
            if param.requires_grad:
                param.requires_grad = False

        print_memory_usage()
        model.eval()
        # result = evaluate_model(
        #     model, tokenizer, args.model_id, "piqa,boolq", limit=100
        # )
        # boolq = result["results"]["boolq"]["acc"]
        # piqa = result["results"]["piqa"]["acc"]
        # print(boolq, piqa)
        # with open("outputs/intrain_eval.log", "a+") as f:
        #     f.write(
        #         f"{args.model_id}: {args.binarization_method} {args.outlier_fraction} {args.train_steps} {args.dataset} {module_name} {boolq} {piqa}\n"
        #     )
        
        save_bnn(
            model,
            args.model_save_dir
            + f"/{args.granularity}/{args.model_id.replace('/','_')}_o{args.outlier_fraction}_{module_name}",
        )

    evaluate_model(model, tokenizer, args.model_id, 'llmqat', limit=200, eval_ppl=True)



def main(args):
    if 'openlm' in args.model_id:
        tokenizer = LlamaTokenizer.from_pretrained(args.model_id, device_map="auto")
        model = LlamaForCausalLM.from_pretrained(args.model_id, device_map="auto")
    else:
        tokenizer = AutoTokenizer.from_pretrained(args.model_id, device_map="auto")
        model = AutoModelForCausalLM.from_pretrained(args.model_id, device_map="auto")

    # Enable gradient checkpointing and prepare model for k-bit training
    # model.gradient_checkpointing_enable()
    model = prepare_model_for_training(model)
    tokenizer.pad_token = tokenizer.eos_token

    # Load dataset
    print("prepare training data")
    data = get_qat_dataset(args.dataset, tokenizer, args.data_percent)

    if args.granularity == "per_block":
        if isinstance(model, OPTForCausalLM):
            ordered_name_modules = [
                (f"block{i}", _) for i, _ in enumerate(model.model.decoder.layers)
            ]
        else:
            # LLaMA
            ordered_name_modules = [
                (f"block{i}", _) for i, _ in enumerate(model.base_model.layers)
            ]
        if args.order == "reverse":
            ordered_name_modules = ordered_name_modules[::-1]
    elif args.granularity == "per_linear":
        ordered_name_modules = []
        for name, module in model.named_modules():
            if isinstance(module, nn.Linear):
                ordered_name_modules.append((name, module))
    elif args.granularity == "whole_model":
        ordered_name_modules = [("whole_model", model)]
        # ordered_name_modules = [('whole_model', _) for i, _ in enumerate(model.base_model.layers) if i != 0 and i != 25]
        # print(ordered_name_modules)
    else:
        raise NotImplementedError

    # initial_state_dict = copy.deepcopy(model.state_dict())

    iterative_train(model, ordered_name_modules, data, tokenizer)

    # updated_state_dict = model.state_dict()
    # for (layer_name, initial_param), (_, updated_param) in zip(initial_state_dict.items(), updated_state_dict.items()):
    #     if not torch.equal(initial_param, updated_param):
    #         print(f"{layer_name} has been updated.")
    #     else:
    #         print(f"{layer_name} has NOT been updated.")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Model Training Script")
    parser.add_argument(
        "--model_id",
        type=str,
        default="facebook/opt-350m",
        help="Pretrained model ID",
    )
    parser.add_argument(
        "--granularity",
        type=str,
        default="whole_model",
        choices=["per_block", "per_linear", "whole_model"],
    )
    parser.add_argument(
        "--dataset", type=str, default="Abirate/english_quotes", help="Dataset name"
    )
    parser.add_argument(
        "--data_percent", type=float, default=100, help="Percentage of data to use"
    )
    parser.add_argument(
        "--order", type=str, default="forward", choices=["forward", "reverse"]
    )
    parser.add_argument(
        "-s", "--train_steps", type=int, default=1000, help="Number of training steps"
    )
    parser.add_argument(
        "--binarization_method",
        type=str,
        default="xnor_act_outlier_column",
        choices=[
            "ste",
            "ir",
            "fda",
            "xnor",
            "bireal",
            "ste_outlier",
            "xnor_outlier",
            "xnor_outlier_column",
            "ir_outlier_column",
            "xnor_act_outlier_column",
            "ir_act_outlier_column",
            "fda_act_outlier_column",
            "bireal_act_outlier_column",
            'lowbit_quant',
        ],
    )
    parser.add_argument(
        "--outlier_fraction", type=float, default=0.05, help="Percentage of outliers"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Debug mode (only 10 steps)"
    )
    parser.add_argument(
        "--model_save_dir",
        type=str,
        default="./checkpoints",
        help="saving model to this directory",
    )
    args = parser.parse_args()

    main(args)
