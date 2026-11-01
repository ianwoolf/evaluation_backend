"""Non-secret effective feature configuration for the standalone workbench."""
import os
from fastapi import APIRouter, Depends
from evaluation_backend.runs import authorize


def flag(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower().strip()
    if value not in ('true', 'false', '1', '0'):
        raise ValueError(f'{name} must be true or false')
    return value in ('true', '1')


def scoring_enabled() -> bool:
    # Existing installations retain real scoring unless deployment opts into demo.
    return flag('EVAL_SCORING_ENABLED', True)


def resolve_modes(*, scoring_enabled: bool, reflection_enabled: bool) -> dict:
    return {'scoring_enabled': scoring_enabled, 'reflection_enabled': reflection_enabled,
            'effective_reflection_mode': 'real' if scoring_enabled and reflection_enabled else 'mock'}


def capabilities() -> dict:
    return resolve_modes(scoring_enabled=scoring_enabled(),
                         reflection_enabled=flag('EVAL_REPORT_REFLECTION_ENABLED', False))


router = APIRouter(prefix='/api/v1/evaluations', dependencies=[Depends(authorize)])
router.add_api_route('/capabilities', capabilities, methods=['GET'])
