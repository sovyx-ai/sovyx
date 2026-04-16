/**
 * Static metadata for the 10 supported LLM providers.
 * Used by the onboarding wizard to render provider cards.
 */

export interface ProviderMeta {
  id: string;
  name: string;
  description: string;
  defaultModel: string;
  envVar: string;
  keyPrefix: string;
  keyUrl: string;
  pricing: string;
  local?: boolean;
}

export const PROVIDERS: ProviderMeta[] = [
  {
    id: "ollama",
    name: "Ollama",
    description: "Run models locally. No API key. No cost. Full privacy.",
    defaultModel: "llama3.1:latest",
    envVar: "",
    keyPrefix: "",
    keyUrl: "https://ollama.com",
    pricing: "Free (local hardware)",
    local: true,
  },
  {
    id: "anthropic",
    name: "Anthropic",
    description: "Claude models. Strong reasoning and analysis.",
    defaultModel: "claude-sonnet-4-20250514",
    envVar: "ANTHROPIC_API_KEY",
    keyPrefix: "sk-ant-",
    keyUrl: "https://console.anthropic.com/settings/keys",
    pricing: "$3 / $15 per million tokens",
  },
  {
    id: "openai",
    name: "OpenAI",
    description: "GPT-4o. Versatile, widely supported.",
    defaultModel: "gpt-4o",
    envVar: "OPENAI_API_KEY",
    keyPrefix: "sk-",
    keyUrl: "https://platform.openai.com/api-keys",
    pricing: "$2.50 / $10 per million tokens",
  },
  {
    id: "google",
    name: "Google",
    description: "Gemini 2.5 Pro. 1M context window.",
    defaultModel: "gemini-2.5-pro-preview-03-25",
    envVar: "GOOGLE_API_KEY",
    keyPrefix: "AI",
    keyUrl: "https://aistudio.google.com/apikey",
    pricing: "$1.25 / $10 per million tokens",
  },
  {
    id: "xai",
    name: "xAI",
    description: "Grok models by xAI.",
    defaultModel: "grok-2",
    envVar: "XGROK_API_KEY",
    keyPrefix: "xai-",
    keyUrl: "https://console.x.ai",
    pricing: "$2 / $10 per million tokens",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    description: "Strong reasoning at low cost.",
    defaultModel: "deepseek-chat",
    envVar: "DEEPSEEK_API_KEY",
    keyPrefix: "sk-",
    keyUrl: "https://platform.deepseek.com/api_keys",
    pricing: "$0.14 / $0.28 per million tokens",
  },
  {
    id: "mistral",
    name: "Mistral",
    description: "European AI. Fast and capable.",
    defaultModel: "mistral-large-latest",
    envVar: "MISTRAL_API_KEY",
    keyPrefix: "",
    keyUrl: "https://console.mistral.ai/api-keys",
    pricing: "$2 / $6 per million tokens",
  },
  {
    id: "groq",
    name: "Groq",
    description: "Ultra-fast inference. Free tier available.",
    defaultModel: "llama-3.1-70b-versatile",
    envVar: "GROQ_API_KEY",
    keyPrefix: "gsk_",
    keyUrl: "https://console.groq.com/keys",
    pricing: "Free tier / $0.59 per million tokens",
  },
  {
    id: "together",
    name: "Together",
    description: "Open-source models at scale.",
    defaultModel: "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    envVar: "TOGETHER_API_KEY",
    keyPrefix: "",
    keyUrl: "https://api.together.ai/settings/api-keys",
    pricing: "$0.88 per million tokens",
  },
  {
    id: "fireworks",
    name: "Fireworks",
    description: "Fast open-source model hosting.",
    defaultModel: "accounts/fireworks/models/llama-v3p1-70b-instruct",
    envVar: "FIREWORKS_API_KEY",
    keyPrefix: "fw_",
    keyUrl: "https://fireworks.ai/account/api-keys",
    pricing: "$0.90 per million tokens",
  },
];

export function getProvider(id: string): ProviderMeta | undefined {
  return PROVIDERS.find((p) => p.id === id);
}
