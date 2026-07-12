# Ollama Cloud Models - Quick Start Guide

## What Are Ollama Cloud Models?

Ollama provides access to large models hosted in the cloud that would be too big to run locally. These are accessed through your local Ollama installation using special `:cloud` tags.

## How to Use Ollama Cloud Models

### Step 1: Make Sure Ollama is Running

```bash
# Windows: Start Ollama (usually auto-starts)
# Or manually: ollama serve
```

### Step 2: Run the Cloud Model

Ollama cloud models use special tags like `:cloud` or `:1t-cloud`:

```bash
# Kimi-K2 (1 trillion parameters!)
ollama run kimi-k2:1t-cloud

# DeepSeek-V3.1 (671B parameters with thinking mode)
ollama run deepseek-v3.1

# GPT-OSS (OpenAI's open weights)
ollama run gpt-oss:120b-cloud

# Kimi-K2.7-Code (coding specialist, thinking-capable)
ollama run kimi-k2.7-code:cloud
```

### Step 3: Use in Your Wellness Bot

Once you've run the model with Ollama, use the **exact same name** in your bot:

**In Admin GUI → Advanced Model tab:**

**For Psychological Profiling:**

- Model name: `kimi-k2:1t-cloud`
- Or: `deepseek-v3.1`
- Or: `kimi-k2.7-code:cloud`

**For Main Chat:**

- Same - just type the model name with its cloud tag

## Available Ollama Cloud Models

| Model                    | Tag           | Size              | Best For                           |
| ------------------------ | ------------- | ----------------- | ---------------------------------- |
| `kimi-k2:1t-cloud`       | `:1t-cloud`   | 1 Trillion params | General reasoning, coding          |
| `deepseek-v3.1`          | (default)     | 671B              | Thinking mode, complex reasoning   |
| `kimi-k2.7-code:cloud`   | `:cloud`      | MoE               | Code generation, thinking mode     |
| `gpt-oss:120b-cloud`     | `:120b-cloud` | 120B              | General tasks, OpenAI open weights |

**Important:** The cloud tag (like `:1t-cloud`) tells Ollama to use the cloud-hosted version instead of downloading locally.

## Finding Cloud Model Tags

To see all available tags for a model:

```bash
# List all variants of a model
ollama list | grep kimi-k2
ollama list | grep deepseek
```

Or check Ollama's website:

- https://ollama.com/library/kimi-k2
- https://ollama.com/library/deepseek-v3.1
- https://ollama.com/library/qwen3-coder
- https://ollama.com/library/gpt-oss

## Example: Using Kimi-K2 for Psych Profiling

### Terminal (one-time):

```bash
# This downloads model metadata and verifies cloud access
ollama run kimi-k2:1t-cloud
# You can exit with Ctrl+C after it loads
```

### In Wellness Bot:

1. Open Admin GUI
2. Go to Advanced Model tab
3. Psych Profile Model: `kimi-k2:1t-cloud`
4. Click "Save Psych Model Settings"
5. Select a user and click "Analyze Psych Profile"

The bot will now use the 1 trillion parameter Kimi-K2 model hosted in Ollama's cloud!

## Cloud Model Variants

### DeepSeek-V3.1

```bash
ollama run deepseek-v3.1              # Default (likely cloud)
ollama run deepseek-v3.1:latest       # Latest version
```

### Kimi-K2

```bash
ollama run kimi-k2:1t-cloud          # 1 trillion parameter cloud version
ollama run kimi-k2:latest            # Latest (may be smaller local version)
```

### Qwen3-Coder

```bash
ollama run qwen3-coder:30b           # 30B local version (smaller, downloadable)
ollama run qwen3-coder-next:cloud    # cloud version (next-gen)
```

### Kimi-K2.7-Code

```bash
ollama run kimi-k2.7-code:cloud      # coding specialist, thinking-capable
```

### GPT-OSS

```bash
ollama run gpt-oss:20b               # 20B local version
ollama run gpt-oss:120b-cloud        # 120B cloud version
```

## How It Works

1. **You run** `ollama run kimi-k2:1t-cloud`
2. **Ollama checks** if model is downloaded locally
3. **For `:cloud` tags**, Ollama connects to cloud infrastructure
4. **Model runs** in Ollama's cloud, responses stream to your machine
5. **Your bot** calls `localhost:11434` as normal
6. **Ollama routes** cloud-tagged models to cloud automatically

## Pricing & Limits

- Check Ollama's pricing page for current rates
- Cloud models may have usage limits or costs
- Authentication handled through Ollama (no separate API keys needed for Ollama cloud)

## Troubleshooting

### "Model not found"

```bash
# Make sure you include the cloud tag
ollama run kimi-k2:1t-cloud  # ✅ Correct
ollama run kimi-k2            # ❌ May try to download locally
```

### "Cannot connect to cloud"

- Check your internet connection
- Verify Ollama is updated: `ollama --version`
- Check Ollama status: `ollama list`

### Bot shows "404 Not Found"

- Make sure you ran `ollama run <model>:<cloud-tag>` first
- Use the exact tag in the bot (e.g., `kimi-k2:1t-cloud`)
- Restart Ollama if needed

## Recommended Cloud Models for Wellness Bot

### Best for Psychological Profiling (Accuracy Priority)

1. **`kimi-k2:1t-cloud`** - 1T parameters, excellent reasoning
2. **`deepseek-v3.1`** - 671B with thinking mode
3. **`kimi-k2.7-code:cloud`** - coding specialist with thinking mode

### Best for Daily Chat (Speed & Cost)

1. **Local models** - Use `llama3.2` or similar (free, private, fast)
2. **`deepseek-v3.1`** - If you need cloud quality
3. **`kimi-k2:1t-cloud`** - Best quality cloud option

### Best for Code Analysis

1. **`kimi-k2.7-code:cloud`** - Specialized for code
2. **`deepseek-v3.1`** - Great at reasoning about code
3. **`kimi-k2:1t-cloud`** - General excellence includes coding

## Quick Reference

| What You Type in Bot     | Ollama Command to Run First         |
| ------------------------ | ----------------------------------- |
| `kimi-k2:1t-cloud`       | `ollama run kimi-k2:1t-cloud`       |
| `deepseek-v3.1`          | `ollama run deepseek-v3.1`          |
| `kimi-k2.7-code:cloud`   | `ollama run kimi-k2.7-code:cloud`   |
| `gpt-oss:120b-cloud`     | `ollama run gpt-oss:120b-cloud`     |

---

**Updated:** October 8, 2025
**Key Insight:** Cloud models need `:cloud` or `:1t-cloud` tags!
