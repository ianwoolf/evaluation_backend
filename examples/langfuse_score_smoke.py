"""Explicit synthetic score delivery test, not a real Guardrail evaluation."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from langfuse import Langfuse, propagate_attributes
from evaluation_backend.config import load_config, LangfuseConfig
from evaluation_backend.experiment import Settings
from evaluation_backend.platforms.langfuse import LangfusePlatform
from evaluation_backend.scorers import risk_classification


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='.env')
    parser.add_argument('--dataset-id', required=True)
    parser.add_argument('--experiment-id', default='dev_1')
    args = parser.parse_args()
    load_config(args.config)
    config = LangfuseConfig.from_env()
    client = Langfuse(public_key=config.public_key, secret_key=config.secret_key, base_url=config.base_url)
    platform = LangfusePlatform(client=client, config=config)
    settings = Settings(args.dataset_id, args.experiment_id, [], datetime.now(timezone.utc))
    dataset = platform.get_dataset(settings)
    batch_id = uuid.uuid4().hex
    score_ids = []
    try:
        for item in platform.datasets[dataset.id].items:
            # Always-safe is a deliberately synthetic prediction, independent of reference.
            output = {'has_issue': False}
            score = risk_classification(input=item.input, output=output, reference=item.expected_output)
            metadata = {'experiment_id': args.experiment_id, 'test_only': True,
                        'test_batch_id': batch_id, 'dataset_id': dataset.id, 'dataset_item_id': item.id}
            with propagate_attributes(tags=['experiment_id: ' + args.experiment_id, 'test_only']), client.start_as_current_observation(name='runner-score-smoke', input=item.input, output=output, metadata=metadata) as span:
                score_id = str(uuid.uuid4())
                client.create_score(score_id=score_id, trace_id=span.trace_id, observation_id=span.id,
                                    name='dev_risk_classification', value=score.value, data_type='NUMERIC',
                                    metadata=metadata, comment='Synthetic delivery test. ' + score.reason)
                score_ids.append(score_id)
        client.flush()
        # Score ingestion is asynchronous: verify each score through the authenticated read API.
        pending = set(score_ids)
        deadline = time.monotonic() + 60
        while pending and time.monotonic() < deadline:
            for ident in list(pending):
                try:
                    rows = client.api.scores_v3.get_many_v3(id=ident, fields='details').data
                    if not rows:
                        continue
                    stored = rows[0]
                except Exception as error:
                    if getattr(error, 'status_code', None) == 404:
                        continue
                    raise
                assert stored.metadata['experiment_id'] == args.experiment_id
                assert stored.name == 'dev_risk_classification'
                pending.remove(ident)
            if pending:
                time.sleep(2)
        if pending:
            raise RuntimeError('score readback timeout')
        print(json.dumps({'experiment_id': args.experiment_id, 'batch_id': batch_id,
                          'verified_scores': len(score_ids), 'test_only': True}))
    finally:
        client.shutdown()


if __name__ == '__main__':
    main()
