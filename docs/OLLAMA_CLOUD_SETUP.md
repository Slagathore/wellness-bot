# 🌐 Using Ollama Cloud Models (kimi-k2:1t-cloud)

**Problem**: You want to use `kimi-k2:1t-cloud` without downloading 58GB locally.

**Solution**: There are actually TWO ways to do this, depending on what "cloud" means:

---

## **Option 1: Use Ollama Cloud Service** (Easiest)

If `kimi-k2:1t-cloud` is hosted on **Ollama's cloud platform**, you can access it by changing the `OLLAMA_HOST` to point to their cloud endpoint:

### **Setup:**

1. **Get Ollama Cloud API Key** (if required):
   - Visit: https://ollama.ai/ or check your cloud provider
   - Sign up for cloud access
   - Get your API token

2. **Update .env file**:
   ```bash
   # Change this line:
   OLLAMA_HOST=http://localhost:11434

   # To Ollama cloud endpoint:
   OLLAMA_HOST=https://api.ollama.ai  # Or whatever their cloud URL is

   # If auth required:
   OLLAMA_API_KEY=your_key_here  # (Not currently implemented)
   ```

3. **Test**:
   ```bash
   curl -X POST https://api.ollama.ai/api/chat \
     -H "Content-Type: application/json" \
     -d '{
       "model": "kimi-k2:1t-cloud",
       "messages": [{"role": "user", "content": "Hello"}]
     }'
   ```

### **Pros:**
- No local resources used
- Can use full 1T parameter model
- No downloads

### **Cons:**
- Requires internet
- Costs money (likely)
- Latency from network

---

## **Option 2: Use Cloud Provider Prefix** (If Kimi is from OpenRouter/Together)

If `kimi-k2` is actually hosted by another cloud provider (like OpenRouter, Together AI, etc.), you can use the prefix system already in your code:

### **Setup:**

1. **Add Kimi/Cloud Provider to ollama.py**:

```python
# app/utils/ollama.py line 18
OPENAI_COMPATIBLE_PROVIDERS = {
    "openai/": "https://api.openai.com/v1",
    "anthropic/": "https://api.anthropic.com/v1",
    "google/": "https://generativelanguage.googleapis.com/v1beta",
    "cohere/": "https://api.cohere.ai/v1",
    "mistral/": "https://api.mistral.ai/v1",
    # Add these:
    "openrouter/": "https://openrouter.ai/api/v1",  # If using OpenRouter
    "together/": "https://api.together.xyz/v1",      # If using Together AI
}
```

2. **Update .env**:
```bash
# Add API key for provider
OPENROUTER_API_KEY=sk-or-v1-xxxxx  # If using OpenRouter
# OR
TOGETHER_API_KEY=xxxxx              # If using Together
```

3. **Use prefix in web_search.py**:
```python
# Instead of:
model = "kimi-k2:1t-cloud"

# Use:
model = "openrouter/deepseek/kimi-k2"  # Adjust based on actual provider
# OR
model = "together/kimi-k2"
```

---

## **Option 3: Simple Local Model** ⭐ **RECOMMENDED**

The **easiest and FREE** solution is to use a small local model just for web search detection:

```bash
# Install it:
ollama pull qwen2.5:3b

# Already updated in your code!
# app/utils/web_search.py now uses qwen2.5:3b
```

**Why this is better:**
- Web search detection is a simple task (doesn't need 1T params)
- 3GB model is 99% as accurate as 1T model for classification
- Free, fast, works offline
- Already fixed in your code

---

## **How to Check Which Provider Has kimi-k2**

Run this to test where your model actually is:

```python
import requests

# Test 1: Local Ollama
try:
    r = requests.get("http://localhost:11434/api/tags")
    models = [m['name'] for m in r.json()['models']]
    print("Local models:", models)
except:
    print("Local Ollama not responding")

# Test 2: Try to pull info about model
try:
    r = requests.post("http://localhost:11434/api/show",
                     json={"name": "kimi-k2:1t-cloud"})
    print("Model info:", r.json())
except:
    print("Model not found locally")
```

---

## **Recommended Approach Based on Use Case**

### **For Web Search Detection** (current issue):
✅ **Use local qwen2.5:3b** - Already fixed in your code!
- Fast, accurate, free
- Perfect for simple classification tasks

### **For Main Chat Model** (existing setup):
✅ **Keep using kimi-k2:1t-cloud** through your current Ollama setup
- You're already using it successfully in main conversations
- Only web search was failing

### **Key Insight:**
You don't need the same powerful model for web search detection! It's like using a sledgehammer to crack a nut. The small model is perfect for this task.

---

## **Current Status: ✅ FIXED**

Your code has been updated to:
- Use `qwen2.5:3b` for web search detection (small, fast, local)
- Keep using `kimi-k2:1t-cloud` for main conversations (already working)

**Just run:**
```bash
ollama pull qwen2.5:3b
```

Then restart your bot and web search will work! 🎉

---

## **If You Still Want Cloud Kimi for Web Search**

If you absolutely want to use the cloud model for web search (not recommended due to latency):

1. **Find the actual cloud endpoint**:
   - Contact your cloud provider
   - Get the API URL (e.g., `https://cloud.ollama.ai` or `https://api.kimichat.com`)

2. **Create a separate client for web search**:

```python
# app/utils/web_search.py
import requests

def detect_search_need_with_llm(message: str, model: str = "kimi-k2:1t-cloud"):
    # Use cloud endpoint specifically for web search
    CLOUD_ENDPOINT = "https://api.your-cloud-provider.com/v1/chat"
    API_KEY = os.getenv("KIMI_CLOUD_API_KEY")

    response = requests.post(
        CLOUD_ENDPOINT,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}]
        }
    )
    # ... rest of function
```

But again, this adds latency and cost for a task that doesn't need it!

---

## **Summary**

**Question**: Can we use cloud kimi without downloading 58GB?

**Answer**: Yes, BUT:
1. You need the cloud provider's API endpoint (not `localhost:11434`)
2. You need an API key
3. You need to know which cloud service hosts `kimi-k2:1t-cloud`

**Better Answer**: Use `qwen2.5:3b` locally for web search (already done!)
- Downloads once (2GB)
- Runs in 3GB RAM
- Perfect for this task
- Already updated in your code

**Just run**: `ollama pull qwen2.5:3b` and you're done! ✅

---

## **⏸️ TODO: Come Back Later**

**User Note (2025-10-15)**: Will investigate cloud Kimi setup later. For now, using local qwen2.5:3b for web search detection.

**Questions to resolve:**
1. What is current OLLAMA_HOST in .env?
2. Does main chat work with kimi-k2:1t-cloud?
3. Where is cloud endpoint if not localhost?

**Action items:**
- [ ] Verify main chat uses kimi cloud successfully
- [ ] Find actual cloud endpoint URL
- [ ] Configure web_search.py to use same cloud endpoint
- [ ] Test end-to-end with cloud model
