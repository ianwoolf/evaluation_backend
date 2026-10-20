def create_platform(name):
    if name == "langfuse":
        from evaluation_backend.platforms.langfuse import LangfusePlatform
        return LangfusePlatform()
    raise ValueError("platform adapter not implemented: " + name)
