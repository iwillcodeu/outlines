import math
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import torch

if TYPE_CHECKING:
    from outlines.models.transformers import KVCacheType, Transformers


class Sequence:
    """Represents a sequence generation method."""

    def __init__(self, model: "Transformers", max_tokens: Optional[int] = None):
        """Create a `Sequence` instance.

        Parameters
        ----------
        model
            The instance of the model used to generate next-token probabilities.
        max_tokens
            The maximum number of tokens that will be generated if no termination
            condition is met.

        """
        self.model = model
        self.device = model.device
        self.max_tokens = max_tokens
        self.pad_token_id = torch.tensor(
            model.tokenizer.pad_token_id, device=model.device
        )

    def create_proposal(
        self, generated_token_ids: torch.LongTensor, logits: torch.DoubleTensor
    ) -> torch.DoubleTensor:
        """Create a new proposal from the next-token logits."""
        return logits

    def is_finished(self, token_ids: torch.LongTensor) -> torch.BoolTensor:
        """Determine whether we should stop the generation."""
        raise NotImplementedError(
            "`Sequence.is_finished` must be implemented by subclasses."
        )

    def postprocess_completions(self, completions: List[str]) -> List[str]:
        return completions

    def step(
        self,
        rng: torch.Generator,
        num_prompt_tokens: int,
        token_ids: torch.LongTensor,
        attention_mask: torch.LongTensor,
        samples: int = 1,
        past_key_values: Optional["KVCacheType"] = None,
    ) -> Tuple[torch.LongTensor, torch.FloatTensor, Optional["KVCacheType"]]:
        """Generate one or several tokens that complete the input sequence.

        The sampling step consists in using a model to generate next-token
        logits and then sample `samples`-many new tokens from a categorical
        distribution parametrized by these logits.

        Parameters
        ----------
        rng
            NumPy random number Generator instance.
        num_prompt_tokens
            The number of tokens in the prompt.
        token_ids
            The token sequences.  It has dimensions ``(n_seqs, n)`` for
            some sequence length ``n <= num_prompt_tokens``.
        samples
            The number of continuations to sample from the next-token probability
            distribution.

        Returns
        -------
        A tuple with an array of shape ``(samples, n_seqs, 1)``
        that contains the completed sequences (i.e. input token IDs and
        generated token IDs) and an array of shape
        ``(samples, n_seqs, vocab_size)`` that contains the next token
        probabilities.

        """
        probs, past_key_values = self.model.forward(
            token_ids, attention_mask, past_key_values
        )
        probs = self.create_proposal(token_ids[:, num_prompt_tokens:], probs)
        probs = torch.nn.functional.softmax(probs, dim=-1)

        assert probs.shape[:-1] == token_ids.shape[:-1]

        next_token_ids = vectorized_random_choice(rng, probs, samples).unsqueeze(-1)
        probs = torch.broadcast_to(probs, (samples,) + probs.shape)

        return next_token_ids, probs, past_key_values

    def expand_attention_mask(
        self, attention_mask: torch.LongTensor
    ) -> torch.LongTensor:
        """Expand the attention mask after the last completion.

        Parameters
        ----------
        attention_mask
            An attention mask with shape ``(n_seqs, attention_mask_len)``.

        Returns
        -------
        A new attention mask with shape ``(n_seqs, attention_mask_len + 1)``.

        """
        attention_mask = torch.concatenate(
            [
                attention_mask,
                torch.ones(attention_mask.shape[:-1] + (1,), device=self.device),
            ],
            axis=-1,
        )
        return attention_mask

    @torch.inference_mode()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        samples: int = 1,
        rng: Optional[torch.Generator] = None,
    ) -> Union[str, List[str]]:
        """Generate a new sequence given a prompt.

        Parameters
        ----------
        prompt
            The input prompt.
        samples
            The number of samples to generate for each prompt.

        Returns
        -------
        The full sequence that contains the prompts and the generated string.

        """

        token_ids, attention_mask = self.model.tokenizer.encode(prompt)

        token_ids = token_ids.squeeze(0)
        attention_mask = attention_mask.squeeze(0)

        token_ids = token_ids.to(self.device)
        attention_mask = attention_mask.to(self.device)

        if rng is None:
            rng = torch.Generator(device=self.device)
            rng.seed()

        orig_batch_shape = token_ids.shape[:-1]
        num_prompt_tokens = token_ids.shape[-1]

        token_ids = torch.broadcast_to(token_ids, (samples,) + token_ids.shape)
        attention_mask = torch.broadcast_to(
            attention_mask, (samples,) + attention_mask.shape
        )

        # We flatten the original batch and sample dimensions so that the
        # resulting shape we work in is simply `(num_of_sequences, tokens)`
        batch_size = samples * math.prod(orig_batch_shape)
        token_ids = token_ids.reshape((batch_size, num_prompt_tokens))
        attention_mask = attention_mask.reshape((batch_size, num_prompt_tokens))

        is_finished = torch.zeros(batch_size, dtype=torch.bool, device=self.device)
        unfinished_past_key_values = None

        while True:
            num_generated_tokens = token_ids.shape[-1] - num_prompt_tokens
            if torch.all(is_finished) or num_generated_tokens == self.max_tokens:
                break

            is_not_finished = ~is_finished

            # Draw samples only for the sequences that aren't finished
            unfinished_token_ids = token_ids[is_not_finished]
            unfinished_attention_mask = attention_mask[is_not_finished]
            unfinished_next_token_ids, _, past_key_values = self.step(
                rng,
                num_prompt_tokens,
                unfinished_token_ids,
                unfinished_attention_mask,
                past_key_values=unfinished_past_key_values,
            )
            unfinished_next_token_ids = unfinished_next_token_ids.squeeze(0)

            # Create an array for the next tokens of every sequence, including
            # the finished ones (but pad them)
            next_token_ids = torch.full(
                (batch_size, 1), self.pad_token_id, device=self.device
            )
            next_token_ids[is_not_finished] = unfinished_next_token_ids

            token_ids = torch.concatenate([token_ids, next_token_ids], axis=-1)

            attention_mask = self.expand_attention_mask(attention_mask)

            local_is_finished = self.is_finished(
                token_ids[is_not_finished][:, num_prompt_tokens:]
            ).flatten()

            is_finished[is_not_finished] = local_is_finished

            if past_key_values:
                unfinished_past_key_values = tuple(
                    tuple(vv[~local_is_finished] for vv in v) for v in past_key_values
                )

        result = self.model.tokenizer.decode(token_ids[:, num_prompt_tokens:])
        result = self.postprocess_completions(result)

        if len(result) == 1:
            return result[0]

        return result


def vectorized_random_choice(
    rng: torch.Generator,
    p: torch.FloatTensor,
    samples: int = 1,
):
    """Vectorized implementation of `np.random.choice`.

    `np.random.choice` does not support arrays of probability. This implements
    the equivalent of this function where the `p` argument can be a matrix.

    Note
    ----
    `torch.searchsorted` may be more efficient, but it is not implemented for
    every backend, for instance MPS.

    Parameters
    ----------
    rng
        Torch random number Generator instance
    p
        An array of probability of shape `(num_probability_vectors, num_items)`
        that must sum to 1.
    samples
        The number of samples to take for each probability vector.

    Returns
    -------
    An array of shape `(num_samples, batch_size)`

    """
    cumsum = torch.unsqueeze(p.cumsum(axis=-1), 0)
    rand = torch.rand(
        (samples,) + p.shape[:-1] + (1,), generator=rng, device=rng.device
    )
    idx = (cumsum < rand).sum(axis=-1)

    return idx
