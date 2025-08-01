from contextlib import ExitStack
from typing import Iterator, Literal, Optional, Tuple, Union

import torch
from transformers import CLIPTextModel, CLIPTokenizer, T5EncoderModel, T5Tokenizer, T5TokenizerFast

from invokeai.app.invocations.baseinvocation import BaseInvocation, invocation
from invokeai.app.invocations.fields import (
    FieldDescriptions,
    FluxConditioningField,
    Input,
    InputField,
    TensorField,
    UIComponent,
)
from invokeai.app.invocations.model import CLIPField, T5EncoderField
from invokeai.app.invocations.primitives import FluxConditioningOutput
from invokeai.app.services.shared.invocation_context import InvocationContext
from invokeai.backend.flux.modules.conditioner import HFEncoder
from invokeai.backend.model_manager import ModelFormat
from invokeai.backend.patches.layer_patcher import LayerPatcher
from invokeai.backend.patches.lora_conversions.flux_lora_constants import FLUX_LORA_CLIP_PREFIX, FLUX_LORA_T5_PREFIX
from invokeai.backend.patches.model_patch_raw import ModelPatchRaw
from invokeai.backend.stable_diffusion.diffusion.conditioning_data import ConditioningFieldData, FLUXConditioningInfo


@invocation(
    "flux_text_encoder",
    title="Prompt - FLUX",
    tags=["prompt", "conditioning", "flux"],
    category="conditioning",
    version="1.1.2",
)
class FluxTextEncoderInvocation(BaseInvocation):
    """Encodes and preps a prompt for a flux image."""

    clip: CLIPField = InputField(
        title="CLIP",
        description=FieldDescriptions.clip,
        input=Input.Connection,
    )
    t5_encoder: T5EncoderField = InputField(
        title="T5Encoder",
        description=FieldDescriptions.t5_encoder,
        input=Input.Connection,
    )
    t5_max_seq_len: Literal[256, 512] = InputField(
        description="Max sequence length for the T5 encoder. Expected to be 256 for FLUX schnell models and 512 for FLUX dev models."
    )
    prompt: str = InputField(description="Text prompt to encode.", ui_component=UIComponent.Textarea)
    mask: Optional[TensorField] = InputField(
        default=None, description="A mask defining the region that this conditioning prompt applies to."
    )

    @torch.no_grad()
    def invoke(self, context: InvocationContext) -> FluxConditioningOutput:
        # Note: The T5 and CLIP encoding are done in separate functions to ensure that all model references are locally
        # scoped. This ensures that the T5 model can be freed and gc'd before loading the CLIP model (if necessary).
        t5_embeddings = self._t5_encode(context)
        clip_embeddings = self._clip_encode(context)
        conditioning_data = ConditioningFieldData(
            conditionings=[FLUXConditioningInfo(clip_embeds=clip_embeddings, t5_embeds=t5_embeddings)]
        )

        conditioning_name = context.conditioning.save(conditioning_data)
        return FluxConditioningOutput(
            conditioning=FluxConditioningField(conditioning_name=conditioning_name, mask=self.mask)
        )

    def _t5_encode(self, context: InvocationContext) -> torch.Tensor:
        prompt = [self.prompt]

        t5_encoder_info = context.models.load(self.t5_encoder.text_encoder)
        t5_encoder_config = t5_encoder_info.config
        assert t5_encoder_config is not None

        with (
            t5_encoder_info.model_on_device() as (cached_weights, t5_text_encoder),
            context.models.load(self.t5_encoder.tokenizer) as t5_tokenizer,
            ExitStack() as exit_stack,
        ):
            assert isinstance(t5_text_encoder, T5EncoderModel)
            assert isinstance(t5_tokenizer, (T5Tokenizer, T5TokenizerFast))

            # Determine if the model is quantized.
            # If the model is quantized, then we need to apply the LoRA weights as sidecar layers. This results in
            # slower inference than direct patching, but is agnostic to the quantization format.
            if t5_encoder_config.format in [ModelFormat.T5Encoder, ModelFormat.Diffusers]:
                model_is_quantized = False
            elif t5_encoder_config.format in [
                ModelFormat.BnbQuantizedLlmInt8b,
                ModelFormat.BnbQuantizednf4b,
                ModelFormat.GGUFQuantized,
            ]:
                model_is_quantized = True
            else:
                raise ValueError(f"Unsupported model format: {t5_encoder_config.format}")

            # Apply LoRA models to the T5 encoder.
            # Note: We apply the LoRA after the encoder has been moved to its target device for faster patching.
            exit_stack.enter_context(
                LayerPatcher.apply_smart_model_patches(
                    model=t5_text_encoder,
                    patches=self._t5_lora_iterator(context),
                    prefix=FLUX_LORA_T5_PREFIX,
                    dtype=t5_text_encoder.dtype,
                    cached_weights=cached_weights,
                    force_sidecar_patching=model_is_quantized,
                )
            )

            t5_encoder = HFEncoder(t5_text_encoder, t5_tokenizer, False, self.t5_max_seq_len)

            if context.config.get().log_tokenization:
                self._log_t5_tokenization(context, t5_tokenizer)

            context.util.signal_progress("Running T5 encoder")
            prompt_embeds = t5_encoder(prompt)

        assert isinstance(prompt_embeds, torch.Tensor)
        return prompt_embeds

    def _clip_encode(self, context: InvocationContext) -> torch.Tensor:
        prompt = [self.prompt]

        clip_text_encoder_info = context.models.load(self.clip.text_encoder)
        clip_text_encoder_config = clip_text_encoder_info.config
        assert clip_text_encoder_config is not None

        with (
            clip_text_encoder_info.model_on_device() as (cached_weights, clip_text_encoder),
            context.models.load(self.clip.tokenizer) as clip_tokenizer,
            ExitStack() as exit_stack,
        ):
            assert isinstance(clip_text_encoder, CLIPTextModel)
            assert isinstance(clip_tokenizer, CLIPTokenizer)

            # Apply LoRA models to the CLIP encoder.
            # Note: We apply the LoRA after the transformer has been moved to its target device for faster patching.
            if clip_text_encoder_config.format in [ModelFormat.Diffusers]:
                # The model is non-quantized, so we can apply the LoRA weights directly into the model.
                exit_stack.enter_context(
                    LayerPatcher.apply_smart_model_patches(
                        model=clip_text_encoder,
                        patches=self._clip_lora_iterator(context),
                        prefix=FLUX_LORA_CLIP_PREFIX,
                        dtype=clip_text_encoder.dtype,
                        cached_weights=cached_weights,
                    )
                )
            else:
                # There are currently no supported CLIP quantized models. Add support here if needed.
                raise ValueError(f"Unsupported model format: {clip_text_encoder_config.format}")

            clip_encoder = HFEncoder(clip_text_encoder, clip_tokenizer, True, 77)

            if context.config.get().log_tokenization:
                self._log_clip_tokenization(context, clip_tokenizer)

            context.util.signal_progress("Running CLIP encoder")
            pooled_prompt_embeds = clip_encoder(prompt)

        assert isinstance(pooled_prompt_embeds, torch.Tensor)
        return pooled_prompt_embeds

    def _clip_lora_iterator(self, context: InvocationContext) -> Iterator[Tuple[ModelPatchRaw, float]]:
        for lora in self.clip.loras:
            lora_info = context.models.load(lora.lora)
            assert isinstance(lora_info.model, ModelPatchRaw)
            yield (lora_info.model, lora.weight)
            del lora_info

    def _t5_lora_iterator(self, context: InvocationContext) -> Iterator[Tuple[ModelPatchRaw, float]]:
        for lora in self.t5_encoder.loras:
            lora_info = context.models.load(lora.lora)
            assert isinstance(lora_info.model, ModelPatchRaw)
            yield (lora_info.model, lora.weight)
            del lora_info

    def _log_t5_tokenization(
        self,
        context: InvocationContext,
        tokenizer: Union[T5Tokenizer, T5TokenizerFast],
    ) -> None:
        """Logs the tokenization of a prompt for a T5-based model like FLUX."""

        # Tokenize the prompt using the same parameters as the model's text encoder.
        # T5 tokenizers add an EOS token (</s>) and then pad to max_length.
        tokenized_output = tokenizer(
            self.prompt,
            padding="max_length",
            max_length=self.t5_max_seq_len,
            truncation=True,
            add_special_tokens=True,  # This is important for T5 to add the EOS token.
            return_tensors="pt",
        )

        input_ids = tokenized_output.input_ids[0]
        tokens = tokenizer.convert_ids_to_tokens(input_ids)

        # The T5 tokenizer uses a space-like character ' ' (U+2581) to denote spaces.
        # We'll replace it with a regular space for readability.
        tokens = [t.replace("\u2581", " ") for t in tokens]

        tokenized_str = ""
        used_tokens = 0
        for token in tokens:
            if token == tokenizer.eos_token:
                tokenized_str += f"\x1b[0;31m{token}\x1b[0m"  # Red for EOS
                used_tokens += 1
            elif token == tokenizer.pad_token:
                # tokenized_str += f"\x1b[0;34m{token}\x1b[0m"  # Blue for PAD
                continue
            else:
                color = (used_tokens % 6) + 1  # Cycle through 6 colors
                tokenized_str += f"\x1b[0;3{color}m{token}\x1b[0m"
                used_tokens += 1

        context.logger.info(f">> [T5 TOKENLOG] Tokens ({used_tokens}/{self.t5_max_seq_len}):")
        context.logger.info(f"{tokenized_str}\x1b[0m")

    def _log_clip_tokenization(
        self,
        context: InvocationContext,
        tokenizer: CLIPTokenizer,
    ) -> None:
        """Logs the tokenization of a prompt for a CLIP-based model."""
        max_length = tokenizer.model_max_length

        tokenized_output = tokenizer(
            self.prompt,
            padding="max_length",
            max_length=max_length,
            truncation=True,
            return_tensors="pt",
        )

        input_ids = tokenized_output.input_ids[0]
        attention_mask = tokenized_output.attention_mask[0]
        tokens = tokenizer.convert_ids_to_tokens(input_ids)

        # The CLIP tokenizer uses '</w>' to denote spaces.
        # We'll replace it with a regular space for readability.
        tokens = [t.replace("</w>", " ") for t in tokens]

        tokenized_str = ""
        used_tokens = 0
        for i, token in enumerate(tokens):
            if attention_mask[i] == 0:
                # Do not log padding tokens.
                continue

            if token == tokenizer.bos_token:
                tokenized_str += f"\x1b[0;32m{token}\x1b[0m"  # Green for BOS
            elif token == tokenizer.eos_token:
                tokenized_str += f"\x1b[0;31m{token}\x1b[0m"  # Red for EOS
            else:
                color = (used_tokens % 6) + 1  # Cycle through 6 colors
                tokenized_str += f"\x1b[0;3{color}m{token}\x1b[0m"
            used_tokens += 1

        context.logger.info(f">> [CLIP TOKENLOG] Tokens ({used_tokens}/{max_length}):")
        context.logger.info(f"{tokenized_str}\x1b[0m")
