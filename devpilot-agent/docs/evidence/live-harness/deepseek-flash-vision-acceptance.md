# DeepSeek Flash image input: local acceptance record

Date: 2026-09-16

## Root cause and scope

The local `deepseek` model preset used the retired preview identifier
`deepseek-v4.1-flash-expires-on-0910`.  In addition, the OpenAI-compatible
provider converted every DeepSeek content-block array into a plain string
before the request was issued.  That conversion discarded `image_url` blocks,
so the model received only an attachment placeholder rather than image bytes.

Commit `2da0a21` retains that string-only compatibility path for legacy
DeepSeek text models and preserves OpenAI-compatible text plus `image_url`
blocks for the current multimodal `deepseek-flash` model.

## Local configuration and restart

The local, untracked `~/.nanobot/config.json` preset named `deepseek` was
changed in place:

- `label`: `DeepSeek V4.1 Flash`
- `model`: `deepseek-flash`
- `provider`: unchanged (`deepseek`)

The existing API key and all other preset/provider fields were retained and
not printed.  A local backup was created before the update.  The running WebUI
then received `/restart` and visibly reported `Restart completed in 2.7s.`;
the health endpoint on port 18790 returned HTTP 200 after restart.

## Automated checks

From `nanobot/`:

```text
pytest tests/providers/test_litellm_kwargs.py -q
108 passed in 0.43s

ruff check nanobot/providers/openai_compat_provider.py tests/providers/test_litellm_kwargs.py
All checks passed!
```

The added regression covers a user message containing both `text` and
`image_url` blocks for `deepseek-flash`; the pre-existing `deepseek-chat`
string-content compatibility test remains in the same suite.

## Real WebUI verification

A repository-owned, non-personal `nanobot_logo.png` was uploaded in the local
WebUI with the active preset shown as `DeepSeek V4.1 Flash`.  The prompt was:

```text
请用一句话说明这张图片里最明显的主体是什么。
```

The live DeepSeek response identified the orange cat-head icon and the
`nanobot` wordmark.  This proves the complete local path (WebUI attachment,
context builder, provider request, model response, and rendered chat output)
handled actual image content rather than merely a filename.

Only the repository logo and this test prompt were transmitted to DeepSeek;
none of the user's supplied screenshots or local secrets were uploaded.
