# Using Cloud Models with Ollama Wrapper

The wellness bot now supports both **Ollama's FREE cloud models** and **paid third-party cloud models** (OpenAI, Anthropic, Google, etc.). This allows you to use powerful cloud models without changing your code.

## Ollama's FREE Cloud Models (No API Key Needed!) 🎉

Ollama hosts these models for **free** on `cloud.ollama.com`. Just use the model name directly!

| Model           | Description                     | Best For                      | Context    | Parameters |
| --------------- | ------------------------------- | ----------------------------- | ---------- | ---------- |
| `gpt-oss`       | OpenAI's open-weight GPT models | Reasoning, agentic tasks      | 3.3M       | 20B-120B   |
| `deepseek-v3.1` | DeepSeek's thinking model       | Complex reasoning, coding     | Large      | 671B       |
| `qwen3-coder`   | Alibaba's coding specialist     | Code generation, long context | Very large | 30B-480B   |
| `kimi-k2`       | Mixture-of-experts (MoE) model  | Coding agents, benchmarks     | Large      | MoE        |

**How to use (no setup required!):**

1. In Admin GUI → Advanced Model tab
2. Enter model name: `deepseek-v3.1` or `gpt-oss:120b`
3. That's it! No API key needed ✅

**Recommended FREE cloud models:**

- **Best for Psych Profiling:** `deepseek-v3.1` (has thinking mode!)
- **Best for Coding Help:** `qwen3-coder:480b` (huge context window)
- **Best All-Around:** `gpt-oss:120b` (largest variant)
- **Fastest FREE:** `kimi-k2` (optimized MoE architecture)

**Example: Free Psych Profiling with DeepSeek-V3.1**

1. Open Admin GUI → Advanced Model tab
2. In "Psych Profile Model" field, enter: `deepseek-v3.1`
3. Select a user with 100+ messages
4. Click "Analyze Psych Profile"
5. **Completely free!** No API key needed!

**Available Tags (Model Sizes):**

Many models have different size variants. Use tags to specify:

- `gpt-oss:20b` - 20 billion parameters (faster)
- `gpt-oss:120b` - 120 billion parameters (smarter)
- `qwen3-coder:30b` - 30B version
- `qwen3-coder:480b` - 480B version (massive!)

If no tag specified, Ollama uses the default variant.

## Paid Cloud Providers (Require API Keys)

| Provider  | Prefix       | Example Model                          | API Key Required    |
| --------- | ------------ | -------------------------------------- | ------------------- |
| OpenAI    | `openai/`    | `openai/gpt-4o`                        | `OPENAI_API_KEY`    |
| Anthropic | `anthropic/` | `anthropic/claude-3-5-sonnet-20241022` | `ANTHROPIC_API_KEY` |
| Google    | `google/`    | `google/gemini-1.5-pro`                | `GOOGLE_API_KEY`    |
| Cohere    | `cohere/`    | `cohere/command-r-plus`                | `COHERE_API_KEY`    |
| Mistral   | `mistral/`   | `mistral/mistral-large-latest`         | `MISTRAL_API_KEY`   |

## How to Use Cloud Models

### 1. Set Your API Key

Add the appropriate API key to your environment variables or `.env` file:

```bash
# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."

# Or add to .env file
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

### 2. Select Cloud Model in Admin GUI

In the **Advanced Model** tab, simply type the cloud model name with its prefix:

**For Psychological Profiling:**

- OpenAI: `openai/gpt-4o` or `openai/gpt-4o-mini`
- Anthropic: `anthropic/claude-3-5-sonnet-20241022`
- Google: `google/gemini-1.5-pro`

**For Main Chat:**

- Select from model dropdown or type custom model name with prefix

### 3. Model Will Auto-Route to Cloud API

The system automatically detects cloud model prefixes and:

- Routes requests to the appropriate cloud API
- Uses your API key for authentication
- Converts Ollama parameters to cloud-compatible format
- Returns responses in the same format as local models

## Example: Using GPT-4 for Psych Profiling

1. **Set API key:**

   ```bash
   $env:OPENAI_API_KEY = "sk-proj-..."
   ```

2. **Open Admin GUI** → **Advanced Model** tab

3. **In "Psych Profile Model" field**, enter:

   ```
   openai/gpt-4o
   ```

4. **Click "Save Psych Model Settings"**

5. **Analyze a user** - the profile will be generated using GPT-4!

## Example: Using Claude for Chat

1. **Set API key:**

   ```bash
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```

2. **In main chat model dropdown**, select or type:

   ```
   anthropic/claude-3-5-sonnet-20241022
   ```

3. **Users' messages** will be processed by Claude instead of local Ollama models

## Popular Cloud Models

### OpenAI

- `openai/gpt-4o` - Latest flagship model (fast, multimodal)
- `openai/gpt-4o-mini` - Smaller, cheaper, still very capable
- `openai/gpt-4-turbo` - Previous generation flagship
- `openai/gpt-3.5-turbo` - Fast and cheap for simple tasks

### Anthropic

- `anthropic/claude-3-5-sonnet-20241022` - Best overall (recommended!)
- `anthropic/claude-3-5-haiku-20241022` - Fastest, cheapest
- `anthropic/claude-3-opus-20240229` - Most capable (older)

### Google

- `google/gemini-1.5-pro` - Large context window (2M tokens!)
- `google/gemini-1.5-flash` - Fast and efficient
- `google/gemini-2.0-flash-exp` - Experimental latest version

### Mistral

- `mistral/mistral-large-latest` - Flagship model
- `mistral/mistral-medium-latest` - Balanced performance
- `mistral/mistral-small-latest` - Fast and cheap

### Cohere

- `cohere/command-r-plus` - Best for complex tasks
- `cohere/command-r` - Balanced model
- `cohere/command-light` - Fast and efficient

## Parameter Mapping

Ollama options are automatically converted to cloud API parameters:

| Ollama Option | Cloud API Parameter |
| ------------- | ------------------- |
| `temperature` | `temperature`       |
| `top_p`       | `top_p`             |
| `num_ctx`     | `max_tokens`        |

## JSON Format Support

When using `format="json"` with cloud models:

- The system requests JSON output in the prompt
- If the response isn't valid JSON, it attempts to fix it
- Works with all cloud providers

## Cost Considerations

⚠️ **Cloud models cost money per token!**

**OpenAI GPT-4o pricing (as of 2024):**

- Input: $2.50 per 1M tokens
- Output: $10.00 per 1M tokens

**Anthropic Claude 3.5 Sonnet:**

- Input: $3.00 per 1M tokens
- Output: $15.00 per 1M tokens

**Tips to save money:**

1. Use `gpt-4o-mini` or `claude-3-5-haiku` for routine tasks
2. Use local Ollama models for bulk processing
3. Reserve expensive models (GPT-4o, Claude 3 Opus) for complex analysis
4. Monitor your API usage dashboards

## Fallback Strategy

If a cloud API call fails (no API key, rate limit, etc.):

- The error is logged with details
- The system raises a `RuntimeError` explaining the issue
- You can catch this and fall back to a local model

Example error handling:

```python
try:
    response = generate(prompt=prompt, model="openai/gpt-4o", format="json")
except RuntimeError as e:
    if "API key" in str(e):
        # Fall back to local model
        response = generate(prompt=prompt, model="llama3.2", format="json")
    else:
        raise
```

## Troubleshooting

### "Cloud model requires API key" error

- **Cause:** Environment variable not set
- **Fix:** Set the appropriate `*_API_KEY` environment variable
- **Verify:** `$env:OPENAI_API_KEY` (should show your key)

### "401 Unauthorized" error

- **Cause:** Invalid or expired API key
- **Fix:** Get a new API key from the provider's dashboard
- **Verify:** Test key with a simple curl command

### "429 Rate Limit" error

- **Cause:** Too many requests to cloud API
- **Fix:** Wait a moment, or upgrade your API plan
- **Alternative:** Use a different cloud provider or local model

### Model not found (404) error

- **Cause:** Model name typo or model doesn't exist
- **Fix:** Check provider's documentation for correct model names
- **Example:** It's `gpt-4o` not `gpt4o`, `claude-3-5-sonnet` not `claude-3.5-sonnet`

## Best Practices

1. **Development:** Use local Ollama models (free, private)
2. **Production:** Use cloud models for critical features
3. **Psych Profiling:** Use `claude-3-5-sonnet` or `gpt-4o` (best accuracy)
4. **Routine Chat:** Use local models or `gpt-4o-mini` (cost-effective)
5. **Vision Tasks:** Use `gpt-4o` or `claude-3-5-sonnet` (they have vision)
6. **Long Context:** Use `gemini-1.5-pro` (2M token context!)

## Privacy Considerations

⚠️ **Cloud models send data to external servers!**

- **OpenAI/Anthropic/Google:** Your data is processed on their servers
- **Data retention:** Varies by provider and API tier
- **PHI/PII:** Be cautious with sensitive health information
- **HIPAA compliance:** Most providers require Business Associate Agreement (BAA)

**For maximum privacy:**

- Use local Ollama models only
- Or use cloud models with zero-data-retention API tiers
- Or anonymize/pseudonymize data before sending

## Advanced: Custom Cloud Endpoints

To add a new cloud provider, edit `app/utils/ollama.py`:

```python
# Add to OPENAI_COMPATIBLE_PROVIDERS dict
OPENAI_COMPATIBLE_PROVIDERS = {
    "openai/": "https://api.openai.com/v1",
    "anthropic/": "https://api.anthropic.com/v1",
    "yourprovider/": "https://api.yourprovider.com/v1",  # Add this
}

# Add to env var map in _get_api_key()
env_var_map = {
    "openai/": "OPENAI_API_KEY",
    "anthropic/": "ANTHROPIC_API_KEY",
    "yourprovider/": "YOURPROVIDER_API_KEY",  # Add this
}
```

Then use: `yourprovider/model-name`

---

**Last Updated:** October 8, 2025
**Version:** 1.0 - Initial cloud model support
