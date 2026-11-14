"""Apply shared contracts while retaining explicit feature dependency wiring."""

from functools import wraps
from inspect import signature

from bson import ObjectId
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from fastapi.exceptions import ResponseValidationError
from pydantic import ValidationError

from api.contracts.features import CONTRACTS
from services.serialization import exact_json_numbers


class ContractRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        method = next(iter(kwargs['methods']))
        contract = next((c for c in CONTRACTS if c.method == method and c.path == path), None)
        if contract is None:
            raise ValueError(f'Missing API contract for {method} {path}')
        original_signature = signature(endpoint)

        @wraps(endpoint)
        async def guarded(*args, **values):
            for name in ('data', 'payload'):
                body = values.get(name)
                if contract.request and isinstance(body, contract.request):
                    # Player already consumes its Pydantic model directly.
                    if name == 'data':
                        values[name] = body.model_dump(exclude_unset=True, by_alias=True)
            result = await endpoint(*args, **values)
            if not contract.response:
                return result
            wire = exact_json_numbers(jsonable_encoder(result, custom_encoder={ObjectId: str}))
            try:
                return contract.response.model_validate(wire).model_dump(mode='json', exclude_unset=True)
            except ValidationError as exc:
                raise ResponseValidationError(exc.errors(include_input=False)) from exc

        if contract.request:
            parameters = [
                parameter.replace(annotation=contract.request) if parameter.name in ('data', 'payload') else parameter
                for parameter in original_signature.parameters.values()
            ]
            guarded.__signature__ = original_signature.replace(parameters=parameters)
        kwargs['response_model'] = contract.response
        kwargs['response_model_exclude_unset'] = True
        super().__init__(path, guarded, **kwargs)
