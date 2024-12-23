import os
from enum import Enum

import packaging.version
import torch
import transformers
from datasets import DatasetDict, load_dataset, load_from_disk
from datasets.builder import DatasetGenerationError
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)

from peft import LoraConfig


DEFAULT_CHATML_CHAT_TEMPLATE = "{% for message in messages %}\n{{'<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>' + '\n'}}{% if loop.last and add_generation_prompt %}{{'<|im_start|>assistant\n' }}{% endif %}{% endfor %}"
DEFAULT_ZEPHYR_CHAT_TEMPLATE = "{% for message in messages %}\n{% if message['role'] == 'user' %}\n{{ '<|user|>\n' + message['content'] + eos_token }}\n{% elif message['role'] == 'system' %}\n{{ '<|system|>\n' + message['content'] + eos_token }}\n{% elif message['role'] == 'assistant' %}\n{{ '<|assistant|>\n'  + message['content'] + eos_token }}\n{% endif %}\n{% if loop.last and add_generation_prompt %}\n{{ '<|assistant|>' }}\n{% endif %}\n{% endfor %}"


class ZephyrSpecialTokens(str, Enum):
    user = "<|user|>"
    assistant = "<|assistant|>"
    system = "<|system|>"
    eos_token = "</s>"
    bos_token = "<s>"
    pad_token = "<pad>"

    @classmethod
    def list(cls):
        return [c.value for c in cls]


class ChatmlSpecialTokens(str, Enum):
    user = "<|im_start|>user"
    assistant = "<|im_start|>assistant"
    system = "<|im_start|>system"
    eos_token = "<|im_end|>"
    bos_token = None
    pad_token = "<|endoftext|>"

    @classmethod
    def list(cls):
        return [c.value for c in cls]

def create_dpo_datasets(tokenizer, data_args):
    """
    Create and preprocess a dataset for DPO training.

    Args:
        tokenizer: Tokenizer object with a `clean_chat_template` method.
        data_args: Data arguments object containing dataset information.
        apply_chat_template: Whether to preprocess the dataset using chat templates.

    Returns:
        processed_data: The preprocessed dataset.
    """
    def to_apply_chat_template(messages, tokenizer, remove_system_message=data_args.remove_system_message): 
        # Apply chat template
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False
        )
        
        # Remove the system message if present
        system_message = "<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.<|im_end|>\n"
        if remove_system_message:
            if text.startswith(system_message):
                text = text[len(system_message):]  # Strip the system message from the start
            
        return text

    load_dataset_func = None
    if data_args.is_local_data:
        load_dataset_func = load_from_disk
    else:
        load_dataset_func = load_dataset
    raw_datasets = DatasetDict()
    
    dataset = load_dataset_func(data_args.dataset_name) # If dataset not in splitted format

    if "train" in data_args.splits:
        raw_datasets["train"] = dataset["train"]
    if "test" in data_args.splits:
        raw_datasets["test"] = dataset["test"]
    if "none" in data_args.splits:
        raw_datasets["train"] = dataset
        raw_datasets["test"] = None
    if (not "train" in data_args.splits) and (not "test" in data_args.splits) and (not "none" in data_args.splits):
        raise ValueError(f"Split type {data_args.splits} not recognized as one of test or train.")
        
    def preprocess(row):
        """
        Preprocess each row by applying the chat template to 'chosen' and 'rejected'.
        """
        return {
            "chosen": to_apply_chat_template(row["chosen"], tokenizer),
            "rejected": to_apply_chat_template(row["rejected"], tokenizer)
        }

    # Apply chat template preprocessing if requested
    if data_args.apply_chat_template != "none":
        raw_datasets["train"] = raw_datasets["train"].map(preprocess, batched=False)
        if data_args.splits != "none":
            raw_datasets["test"] = raw_datasets["test"].map(preprocess, batched=False)


    # Using single quotes inside the f-string
    print(f"Dataset size: {len(raw_datasets['train'])}")
    # print(f"Sample row after preprocessing: {raw_datasets['train'][0]}")

    if data_args.splits != "none":
        print(f"Dataset size: {len(raw_datasets['test'])}")
        # print(f"Sample row after preprocessing: {raw_datasets['test'][0]}")

    return raw_datasets["train"], raw_datasets["test"]


def create_and_prepare_model_for_dpo(args, data_args, training_args):
    quant_storage_dtype = None  # Initialize with default value
    
    if args.use_4bit_quantization:
        compute_dtype = getattr(torch, args.bnb_4bit_compute_dtype)
        quant_storage_dtype = getattr(torch, args.bnb_4bit_quant_storage_dtype)

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=args.use_4bit_quantization,
            bnb_4bit_quant_type=args.bnb_4bit_quant_type,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=args.use_nested_quant,
            bnb_4bit_quant_storage=quant_storage_dtype,
        )

        if compute_dtype == torch.float16 and args.use_4bit_quantization:
            major, _ = torch.cuda.get_device_capability()
            if major >= 8:
                print("=" * 80)
                print("Your GPU supports bfloat16, you can accelerate training with the argument --bf16")
                print("=" * 80)

    elif args.use_8bit_quantization:
        bnb_config = BitsAndBytesConfig(load_in_8bit=args.use_8bit_quantization)

    else:
        bnb_config = None  # Ensure bnb_config is always defined

    # Define torch_dtype with fallback logic
    torch_dtype = (
        quant_storage_dtype if quant_storage_dtype and quant_storage_dtype.is_floating_point else torch.float32
    )
    
    # Load the model
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        quantization_config=bnb_config,
        trust_remote_code=True,
        attn_implementation="flash_attention_2" if args.use_flash_attn else "eager",
        torch_dtype=torch_dtype,
    )
    
    # PEFT configuration
    peft_config = None
    if args.use_peft_lora:
        peft_config = LoraConfig(
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            r=args.lora_r,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=args.lora_target_modules.split(",")
            if args.lora_target_modules != "all-linear"
            else args.lora_target_modules,
        )

    # Chat template and tokenizer configuration
    special_tokens = None
    chat_template = None
    if data_args.apply_chat_template == "chatml":
        special_tokens = ChatmlSpecialTokens
        chat_template = DEFAULT_CHATML_CHAT_TEMPLATE
    elif data_args.apply_chat_template == "zephyr":
        special_tokens = ZephyrSpecialTokens
        chat_template = DEFAULT_ZEPHYR_CHAT_TEMPLATE

    if special_tokens is not None:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path,
            pad_token=special_tokens.pad_token.value,
            bos_token=special_tokens.bos_token.value,
            eos_token=special_tokens.eos_token.value,
            additional_special_tokens=special_tokens.list(),
            trust_remote_code=True,
        )
        tokenizer.chat_template = chat_template

        # Handle embedding resizing
        uses_transformers_4_46 = packaging.version.parse(transformers.__version__) >= packaging.version.parse("4.46.0")
        uses_fsdp = os.environ.get("ACCELERATE_USE_FSDP", "").lower() == "true"
        if (bnb_config is not None) and uses_fsdp and uses_transformers_4_46:
            model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8, mean_resizing=False)
        else:
            model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8)
    else:
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)

    return model, peft_config, tokenizer
