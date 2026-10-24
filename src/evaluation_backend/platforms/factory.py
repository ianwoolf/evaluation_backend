def create_platform(name):
    if name == "storage":
        from evaluation_backend.storage.experiment import StoredExperiment
        return StoredExperiment()
    raise ValueError("platform adapter not implemented: " + name)
