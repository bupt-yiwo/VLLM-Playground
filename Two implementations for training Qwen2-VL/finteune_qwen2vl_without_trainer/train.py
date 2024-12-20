from transformers import (
    Qwen2VLForConditionalGeneration,
    AutoProcessor,
    Trainer,
    TrainingArguments,
    TrainerCallback,
    AdamW,
    get_scheduler
)
from torch.utils.data import Dataset,DataLoader
import json
import torch
from util.vision_util import process_vision_info
from util.save_util import write_chat_template,save_all
import sys
import argparse
from accelerate import Accelerator
import random
import numpy as np
import logging
import os
from tqdm.auto import tqdm
import math

parser = argparse.ArgumentParser()
parser.add_argument('--model_dir', type=str, default="/home/zhuyao/Sunpeng/models/qwen_2B_instruct",required=False, help='Pretrained model directory')
parser.add_argument('--output_dir', type=str, default="/home/zhuyao/Sunpeng/finteune_qwen2vl_no_trainer/result",required=False, help='Pretrained model directory')
parser.add_argument('--min_image_tokens', type=int, default=336,required=False, help='min_image_tokens(of a photo)')
parser.add_argument('--max_image_tokens', type=int, default=336,required=False, help='max_image_tokens(of a photo)')
parser.add_argument('--train_data_path', type=str, default="/home/zhuyao/Sunpeng/finetune_qwen2vl/data/pokemen_train.json",required=False, help='Training data file path')
parser.add_argument('--eval_data_path', type=str, default="/home/zhuyao/Sunpeng/finetune_qwen2vl/data/pokemen_eval.json",required=False, help='Eval data file path')
parser.add_argument('--num_train_epochs', type=int, default=3,required=False, help='num_train_epochs')
parser.add_argument('--learning_rate', type=float, default=1e-5,required=False, help='learning_rate')
parser.add_argument('--warmup_steps', type=int, default=0,required=False, help='warmup_steps')
parser.add_argument('--lr_scheduler_type', type=str, default="cosine",required=False, help='lr_scheduler_type')
parser.add_argument('--per_device_train_batch_size', type=int, default=2,required=False, help='per_device_eval_batch_size')
parser.add_argument('--per_device_eval_batch_size', type=int, default=2,required=False, help='per_device_eval_batch_size')
parser.add_argument('--gradient_accumulation_steps', type=int, default=1,required=False, help='gradient_accumulation_steps')
parser.add_argument('--gpu_nums', type=int, default=2,required=False, help='Number of gpu usage')
parser.add_argument('--log_dir', type=str, default="/home/zhuyao/Sunpeng/finteune_qwen2vl_no_trainer/log",required=False, help='log_dir')
parser.add_argument('--log_steps', type=int, default=10,required=False, help='log_steps')
parser.add_argument('--save_steps', type=int, default=10,required=False, help='save_steps')
parser.add_argument('--system_message', type=str, default="You are an expert in the medical field, focused on discussions regarding medical knowledge, diagnostics, treatment plans, and related health issues. You should provide advice and information based on the latest scientific research, clinical practice, and authoritative medical guidelines. When answering questions, ensure that the medical information provided is accurate, reliable, and evidence-based. Do not offer unverified treatment advice. Based on patient history, symptoms, and medical data, make reasonable assumptions and recommendations, and when necessary, suggest further tests or that the patient consults a professional doctor. Your goal is to help users understand medical knowledge and support them in making informed health decisions.", required=False, help='system_message')

args = parser.parse_args()

os.makedirs(args.output_dir, exist_ok=True)  
# logger
log_path = os.path.join(args.log_dir,"log.txt")
if not os.path.exists(log_path):
    os.makedirs(args.log_dir, exist_ok=True)  
    
with open(log_path, 'w', encoding='utf-8') as f:
    f.write("train\n")

logger = logging.getLogger('train_logger')
logger.setLevel(logging.INFO) 

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

file_handler = logging.FileHandler(log_path)
file_handler.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler) 

# set seed
def set_seed(seed: int):
    random.seed(seed)  
    np.random.seed(seed) 
    torch.manual_seed(seed) 
    torch.cuda.manual_seed(seed)  
    torch.cuda.manual_seed_all(seed)  
    torch.backends.cudnn.deterministic = True  
    torch.backends.cudnn.benchmark = False  

set_seed(42)  


# set dataset        
class QwenDataSet(Dataset):  
    def __init__(self, data_path):
        super().__init__()
        with open(data_path, "r") as f:
            self.data = json.load(f)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        return self.data[idx]
    
# set datacollator
def find_assistant_content_sublist_indexes(l):
    start_token_ids = [151644, 77091]
    end_token_id = 151645

    start_indexes = []
    end_indexes = []

    i = 0
    while i < len(l) - 1:
        if l[i:i+2] == start_token_ids:
            start_indexes.append(i+1)
            j = i + 2
            while j < len(l):
                if l[j] == end_token_id:
                    end_indexes.append(j)
                    break
                j += 1
            i = j
        else:
            i += 1

    return list(zip(start_indexes, end_indexes))


class DataCollatorForQwen2VL:
    def __init__(self, processor):
        self.processor = processor

    def __call__(self, batch):
        messages = [m['messages'] for m in batch]
        texts = [
            self.processor.apply_chat_template(
                msg, tokenize=False, add_generation_prompt=False
            )
            for msg in messages
        ]
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self.processor(
            text=texts,
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )

        input_ids_lists = inputs['input_ids'].tolist()
        assert len(messages) == len(input_ids_lists)

        labels_list = []
        for ids_list in input_ids_lists:
            label_ids = [-100] * len(ids_list)
            for begin_end_indexs in find_assistant_content_sublist_indexes(ids_list):
                # Exclude the special tokens for assistant start
                label_ids[
                    begin_end_indexs[0] + 2 : begin_end_indexs[1] + 1
                ] = ids_list[begin_end_indexs[0] + 2 : begin_end_indexs[1] + 1]
            labels_list.append(label_ids)

        labels_ids = torch.tensor(labels_list, dtype=torch.int64)
        inputs['labels'] = labels_ids
        return inputs
    
        
# set model, processor and data
accelerator = Accelerator()    
model = Qwen2VLForConditionalGeneration.from_pretrained(
        args.model_dir,
        torch_dtype=torch.bfloat16,
    )
processor = AutoProcessor.from_pretrained(
        args.model_dir,
        min_pixels=args.min_image_tokens * 28 * 28,
        max_pixels=args.max_image_tokens * 28 * 28,
    )
processor.chat_template = processor.chat_template.replace("You are a helpful assistant.",args.system_message)

# device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
# model.to(device)
for name, param in model.visual.named_parameters():
    param.requires_grad = False
    
train_dataset = QwenDataSet(args.train_data_path)
eval_dataset = QwenDataSet(args.eval_data_path)
data_collator = DataCollatorForQwen2VL(processor)

train_dataloader = DataLoader(
    train_dataset, shuffle=True, batch_size=args.per_device_train_batch_size, collate_fn=data_collator
)
eval_dataloader = DataLoader(
    eval_dataset, batch_size=args.per_device_eval_batch_size, collate_fn=data_collator
)

# train 
optimizer = AdamW(model.parameters(), lr=args.learning_rate)
num_epochs = args.num_train_epochs
num_training_steps = math.floor(num_epochs * len(train_dataloader) /  args.gradient_accumulation_steps)
lr_scheduler = get_scheduler(
    args.lr_scheduler_type,
    optimizer=optimizer,
    num_warmup_steps=args.warmup_steps,
    num_training_steps=num_training_steps,
)

train_dataloader, eval_dataloader, model, optimizer = accelerator.prepare(
     train_dataloader, eval_dataloader, model, optimizer
 )
gradient_accumulation_steps = args.gradient_accumulation_steps  
progress_bar = tqdm(range(num_training_steps))

log_steps = args.log_steps
save_steps = args.save_steps


running_loss = 0.0
step_turn = 0
"""
The gradient accumulation, step updates, and other operations here are manually implemented by me. 
In fact, these operations are already integrated into accelerate. You can refer to the accelerate-related sections in the Code Implementation of Various Approaches for Model Training, specifically in distributed_training.ipynb.
"""                                                                                                                                                                
for epoch in range(num_epochs):
    model.train()
    for step, batch in enumerate(train_dataloader):
        step_turn+=1
        # print(len(train_dataloader))
        # batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss / gradient_accumulation_steps
        accelerator.backward(loss)
        # loss.backward()
        running_loss += loss.item()
        update = step_turn % gradient_accumulation_steps
        now_step = step_turn / gradient_accumulation_steps
        if update == 0:
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            if accelerator.is_main_process: 
                progress_bar.update(1)

        if now_step % log_steps == 0:  
            loss_tensor = torch.tensor([running_loss], device=accelerator.device)
            avg_running_loss = accelerator.gather(loss_tensor).mean().item()
            if accelerator.is_main_process: 
                print(avg_running_loss)
                print(num_training_steps)
                logger.info(f'Epoch {epoch + (step+1)/len(train_dataloader)},Step:{int(now_step)} Loss: {avg_running_loss / log_steps}')
            running_loss = 0
        if now_step % save_steps == 0:
            if accelerator.is_main_process: 
                save_path = os.path.join(args.output_dir, f"step-{int(now_step)}")
                model.save_pretrained(save_path)
        

    # model.eval()
    # eval_loss = 0.0 
    # num_batches = 0
    # for batch in eval_dataloader:
    #     with torch.no_grad():
    #         num_batches+=1
    #         # print(num_batches)
    #         # batch = {k: v.to(device) for k, v in batch.items()}
    #         outputs = model(**batch)
    #         eval_loss += outputs.loss.item()
    # eval_loss_tensor = torch.tensor([eval_loss], device=accelerator.device)
    # avg_eval_loss = accelerator.gather(eval_loss_tensor) 
    # num_batches_tensor = torch.tensor([num_batches], device=accelerator.device)
    # avg_num_batches = accelerator.gather(num_batches_tensor)
    
    # if accelerator.is_main_process:  
    #     global_avg_loss = avg_eval_loss.sum().item() / avg_num_batches.sum().item()
    #     logger.info(f"Eval_Loss: {global_avg_loss}")
    # # output_dir
