DEFAULT_CONFIG = {
    "agent_llm_model": "deepseek-flash",
    "graph_llm_model": "deepseek-flash",
    "agent_llm_provider": "deepseek",
    "graph_llm_provider": "deepseek",
    "agent_llm_temperature": 0.1,
    "graph_llm_temperature": 0.1,
    "api_key": "sk-",  # OpenAI API key
    "anthropic_api_key": "sk-",  # Anthropic API key (optional, can also use ANTHROPIC_API_KEY env var)
    "qwen_api_key": "sk-",  # Qwen API key (optional, can also use DASHSCOPE_API_KEY env var)
    "minimax_api_key": "",  # MiniMax API key (optional, can also use MINIMAX_API_KEY env var)
    "minimax_cn_api_key": "",  # MiniMax CN API key (optional, can also use MINIMAX_CN_API_KEY or MINIMAX_API_KEY env var)
}

# DeepSeek专属接入设置；不修改作者的代理提示词或分析工具。
# temperature的原值0.1保留在DEFAULT_CONFIG中，但思考模式不支持它，实际请求不发送。
DEEPSEEK_RUNTIME_CONFIG = {
    "thinking": "enabled",
    "reasoning_effort": "high",
    "max_tokens": 16384,
    "timeout_seconds": 300,
    "max_retries": 2,
    "invalid_response_retries": 1,
    "invalid_response_retry_delay": 2,
}
