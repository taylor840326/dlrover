import os
import subprocess

import numpy as np
import pytest

torch = pytest.importorskip("torch", minversion="2.0.9")
if torch.version.git_version != "7bcf7da3a268b435777fe87c7794c382f444e86d" or not torch.cuda.is_available():
    pytest.skip("requires pytorch 2.1 stable release", allow_module_level=True)

from peft import LoraConfig, TaskType, get_peft_model  # noqa: E402
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP  # noqa: E402
from torch.nn.parallel import DistributedDataParallel as DDP  # noqa: E402
from torch.utils.data import Dataset  # noqa: E402
from transformers import LlamaConfig  # noqa: E402
from transformers.models.llama.modeling_llama import LlamaDecoderLayer, LlamaForCausalLM  # noqa: E402
from transformers.trainer_utils import EvalPrediction  # noqa: E402

from atorch.common.util_func import find_free_port  # noqa: E402
from atorch.trainer.atorch_args import AtorchArguments  # noqa: E402
from atorch.trainer.atorch_trainer import AtorchTrainer  # noqa: E402
from atorch.utils.import_util import is_coverage_available  # noqa: E402
from atorch.utils.version import torch_version  # noqa: E402

if is_coverage_available():
    import coverage


class LlamaDatset(Dataset):
    def __init__(self, size=100, max_words=30):
        self.size = 100
        self.max_words = max_words

    def __len__(self):
        return self.size

    def __getitem__(self, idx):
        input_ids = torch.ones([self.max_words], dtype=torch.int64)
        labels = torch.ones([self.max_words], dtype=torch.int64)
        attention_mask = torch.ones([self.max_words], dtype=torch.float32)
        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": attention_mask,
        }


def run_atorch_trainer(test_args):
    train_dataset = LlamaDatset(100)
    eval_dataset = LlamaDatset(20)
    test_dataset = LlamaDatset(20)

    atorch_opt = test_args.get("atorch_opt", "fsdp")
    save_by_streaming = test_args.get("save_by_streaming", True)
    use_atorch_dataloader = test_args.get("use_atorch_dataloader", True)
    use_default_data_collator = test_args.get("use_default_data_collator", True)
    async_save = test_args.get("async_save", False)
    peft_type = test_args.get("peft_type", None)

    config = LlamaConfig(
        vocab_size=10,
        hidden_size=32,
        intermediate_size=1,
        num_hidden_layers=4,
        num_attention_heads=4,
        max_position_embeddings=1,
    )
    model = LlamaForCausalLM(config)
    args = AtorchArguments(
        output_dir="/tmp/output_atorch_trainer",
        num_train_epochs=1,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        do_train=True,
        bf16=True,
        save_by_streaming=save_by_streaming,
        save_strategy="steps",
        save_steps=0.4,
        save_total_limit=1,
        evaluation_strategy="steps",
        eval_steps=0.9,
        include_inputs_for_metrics=True,
        logging_strategy="steps",
        logging_steps=0.1,
        logging_nan_inf_filter=False,
        gradient_checkpointing=False,
        atorch_opt=atorch_opt,
        atorch_wrap_cls=(LlamaDecoderLayer,),
        model_input_format="unpack_dict",
        use_atorch_dataloader=use_atorch_dataloader,
        use_default_data_collator=use_default_data_collator,
        async_save=async_save,
        peft_type=peft_type,
        atorch_module_replace=False,
    )

    if args.peft_type == "lora":
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=8,
            lora_alpha=32,
            lora_dropout=0.1,
            target_modules=["q_proj", "v_proj"],
        )
        if args.gradient_checkpointing:
            # Make Lora and gradient checkpointing compatible
            # https://github.com/huggingface/peft/issues/137
            model.enable_input_require_grads()
        model = get_peft_model(model, peft_config)

        args.save_base_model = True
        args.hyper_parameters = {"peft_type": "lora"}

    def compute_metrics(eval_preds: EvalPrediction):
        logits, label_ids, inputs = eval_preds  # logits:[b,s,v], label_ids:[b,s], inputs:[b,s]

        def softmax(logits):
            exp_logits = np.exp(logits - np.expand_dims(np.max(logits, axis=-1), axis=-1))
            return exp_logits / np.expand_dims(np.sum(exp_logits, axis=-1), axis=-1)

        probs = softmax(logits)
        predictions = np.argmax(probs, axis=-1)
        right_num: int = np.sum(predictions == label_ids)
        total_samples = label_ids.shape[0]
        acc = right_num / total_samples
        return {"accuracy": acc}

    args.eval_accumulation_steps = 1

    trainer = AtorchTrainer(
        model=model,
        args=args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=compute_metrics,
    )
    train_result = trainer.train()
    trainer.predict(test_dataset)
    metrics = train_result.metrics
    metrics["train_samples"] = len(train_dataset)
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()
    if args.world_size > 1:
        assert isinstance(trainer.model, FSDP if atorch_opt == "fsdp" else DDP)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="Skip cpu ut, only run on gpu.")
@pytest.mark.skipif(torch_version() < (2, 0, 0), reason="AtorchTrainer need torch2.0 .")  # type: ignore
@pytest.mark.parametrize("atorch_opt", ["fsdp", "ddp"])
@pytest.mark.parametrize("save_by_streaming", [True, False])
@pytest.mark.parametrize("use_atorch_dataloader", [True])
@pytest.mark.parametrize("use_default_data_collator", [True])
@pytest.mark.parametrize("async_save", [True])
@pytest.mark.parametrize("peft_type", [None])
@pytest.mark.parametrize("gpu_num", [1, 4])
def test_atorch_trainer(
    atorch_opt, save_by_streaming, use_atorch_dataloader, use_default_data_collator, async_save, peft_type, gpu_num
):
    # save_by_streaming only works on fsdp training mode.
    if atorch_opt == "ddp" and save_by_streaming:
        pytest.skip()

    # Skip some tests to reduce the time of unit test.
    if gpu_num == 1:
        if (
            save_by_streaming
            or not use_atorch_dataloader
            or not use_default_data_collator
            or async_save
            or not peft_type
        ):
            pytest.skip()
    if async_save:
        if save_by_streaming or not use_atorch_dataloader or not use_default_data_collator:
            pytest.skip()

    test_args = {
        "atorch_opt": atorch_opt,
        "save_by_streaming": save_by_streaming,
        "use_atorch_dataloader": use_atorch_dataloader,
        "use_default_data_collator": use_default_data_collator,
        "async_save": async_save,
        "peft_type": peft_type,
    }

    # Test for AntMonitor
    if os.environ.get("ANTMONITOR_TFEVENT_PATH") is None:
        os.environ["ANTMONITOR_TFEVENT_PATH"] = "/home/admin/logs/tfevent"

    launch_engine = "coverage run" if is_coverage_available() else "python"
    dist_cmd = (
        f"{launch_engine} -m atorch.distributed.run --nnode=1 --nproc_per_node={gpu_num} "
        f"--node_rank=0 --master_port={find_free_port()} {__file__}"
    )

    for k, v in test_args.items():
        if isinstance(v, bool):
            if v:
                dist_cmd += f" --{k}"
        elif v is not None:
            dist_cmd += f" --{k} {v}"

    subprocess.run(dist_cmd, check=True, shell=True)


if __name__ == "__main__":
    import argparse

    ut_cov = None
    if is_coverage_available():
        ut_cov = coverage.Coverage()
        ut_cov.start()

    parser = argparse.ArgumentParser(description="Test atorch trainer.")

    parser.add_argument("--atorch_opt", type=str, help="ATorch optimize method.")
    parser.add_argument(
        "--save_by_streaming", action="store_true", help="Whether to use stream saving when using FSDP."
    )
    parser.add_argument(
        "--use_atorch_dataloader", action="store_true", help="Whether to use auto_accelerate to generate dataloader."
    )
    parser.add_argument(
        "--use_default_data_collator", action="store_true", help="Whether to use default data collator in trainer."
    )
    parser.add_argument(
        "--async_save", action="store_true", help="Whether to use asynchronous saving model and optimizer."
    )
    parser.add_argument("--peft_type", type=str, default=None, help="What peft fintune method to use.")

    test_args = vars(parser.parse_args())

    run_atorch_trainer(test_args)

    if ut_cov is not None:
        ut_cov.stop()
        ut_cov.save()
