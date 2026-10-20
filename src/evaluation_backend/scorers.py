"""Trusted preinstalled scorer library; remote metadata never executes code."""
import math
from importlib.metadata import entry_points

from evaluation_backend.experiment import Dataset, Score, Scorer


def risk_classification(*, input, output, reference, metadata=None):
    if not isinstance(output, dict) or not isinstance(reference, dict):
        raise ValueError('classification requires object output and reference')
    predicted, expected = output.get('has_issue'), reference.get('has_issue')
    if type(predicted) is not bool or type(expected) is not bool:
        raise ValueError('has_issue must be boolean in output and reference')
    reason = 'Correct classification' if predicted == expected else 'False positive' if predicted else 'False negative'
    return Score(float(predicted == expected), reason)


class InstalledScorerLibrary:
    def __init__(self, registry=None):
        self.registry = {'risk-classification': risk_classification} if registry is None else registry

    def get_scorers(self, dataset: Dataset, ids: list[str]) -> list[Scorer]:
        evaluation = dataset.metadata.get('evaluation', {})
        if not isinstance(evaluation, dict):
            raise ValueError('dataset evaluation metadata must be an object')
        configs = evaluation.get('scorers', {})
        if not isinstance(configs, dict):
            raise ValueError('dataset evaluation.scorers must be an object')
        result = []
        for scorer_id in ids:
            config = configs.get(scorer_id)
            if not isinstance(config, dict) or not config.get('version'):
                raise ValueError(f'missing versioned scorer metadata: {scorer_id}')
            threshold = config.get('threshold', .8)
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ValueError('scorer threshold must be finite in [0,1]')
            library = config.get('library')
            function = self.registry.get(library)
            if function is None:
                # Only distributions explicitly installed in the trusted image.
                plugins = list(entry_points(group='evaluation_backend.scorers', name=library)) if isinstance(library, str) else []
                if len(plugins) != 1:
                    raise ValueError(f'scorer library is not installed: {library}')
                function = plugins[0].load()
            if not callable(function):
                raise ValueError('invalid scorer entry point')
            result.append(Scorer(scorer_id, str(config['version']), float(threshold), function))
        return result
