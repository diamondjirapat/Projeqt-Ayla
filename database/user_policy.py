"""Instantiate the same atomic user mutations used by the public web repository."""

import json
import re
from pathlib import Path

MUTATIONS = json.loads(Path(__file__).with_name('user-mutations.json').read_text(encoding='utf-8'))


def validate_user_settings(values: dict) -> None:
    from database.user_settings import SettingsRequest

    if 'settings' in values:
        SettingsRequest.model_validate(values['settings'])
    settings = {key.removeprefix('settings.'): value for key, value in values.items() if key.startswith('settings.')}
    if 'locale' in values:
        settings['locale'] = values['locale']
    SettingsRequest.model_validate(settings)


def user_mutation(name: str, **values) -> dict:
    def render(value):
        if isinstance(value, str):
            if re.fullmatch(r'\{\{\w+\}\}', value):
                return values[value[2:-2]]
            return re.sub(r'\{\{(\w+)\}\}', lambda match: str(values[match[1]]), value)
        if isinstance(value, dict):
            return {render(key): render(item) for key, item in value.items()}
        return value

    return render(MUTATIONS[name])
