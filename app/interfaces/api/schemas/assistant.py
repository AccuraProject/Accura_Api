"""Pydantic models for the assistant interaction endpoints."""

from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantMessageRequest(BaseModel):
    """Payload with the user's free-form message."""

    message: str = Field(..., min_length=1, description="Mensaje que será analizado por el asistente")


class TipoDatoEnum(str, Enum):
    """Enumeración para los tipos de dato soportados en la regla."""

    TEXTO = "Texto"
    NUMERO = "Número"
    DOCUMENTO = "Documento"
    LISTA = "Lista"
    LISTA_COMPLEJA = "Lista compleja"
    TELEFONO = "Teléfono"
    CORREO = "Correo"
    FECHA = "Fecha"
    DEPENDENCIA = "Dependencia"
    VALIDACION_CONJUNTA = "Validación conjunta"
    DUPLICADOS = "Duplicados"


DEPENDENCY_TYPE_HEADERS: dict[str, tuple[str, ...]] = {
    "texto": ("Longitud mínima", "Longitud máxima"),
    "numero": ("Valor mínimo", "Valor máximo", "Número de decimales"),
    "documento": ("Longitud mínima", "Longitud máxima"),
    "lista": ("Lista",),
    "lista compleja": ("Lista compleja",),
    "telefono": ("Longitud mínima", "Código de país"),
    "correo": ("Formato", "Longitud máxima"),
    "fecha": ("Formato", "Fecha mínima", "Fecha máxima"),
}


class AssistantMessageResponse(BaseModel):
    """Structured response describing a single validation rule."""

    nombre_de_la_regla: str = Field(
        ..., alias="Nombre de la regla", min_length=1, description="Nombre identificador de la regla"
    )
    tipo_de_dato: TipoDatoEnum = Field(..., alias="Tipo de dato", description="Tipo de dato que valida la regla")
    campo_obligatorio: bool = Field(
        ..., alias="Campo obligatorio", description="Indica si el campo es obligatorio"
    )
    mensaje_de_error: str = Field(
        ..., alias="Mensaje de error", description="Mensaje que se muestra cuando la validación falla"
    )
    descripcion: str = Field(..., alias="Descripción", description="Descripción detallada de la regla")
    ejemplo: Any = Field(..., alias="Ejemplo", description="Ejemplo representativo que cumple la regla")
    header: list[str] = Field(
        ..., alias="Header", min_length=1, description="Campos disponibles dentro de la regla"
    )
    header_rule: list[str] = Field(
        ...,
        alias="Header rule",
        min_length=1,
        description="Encabezados que intervienen en la validación",
    )
    regla: dict[str, Any] = Field(
        ..., alias="Regla", description="Configuración específica de la regla según el tipo de dato"
    )

    model_config = ConfigDict(
        validate_by_name=True,
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def validate_regla(self) -> "AssistantMessageResponse":
        tipo: TipoDatoEnum | None = self.tipo_de_dato
        regla: Any = self.regla

        if not isinstance(self.header, list) or not self.header:
            raise ValueError("El campo 'Header' debe ser una lista con al menos un elemento.")
        for item in self.header:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("Cada elemento dentro de 'Header' debe ser una cadena no vacía.")

        if not isinstance(self.header_rule, list) or not self.header_rule:
            raise ValueError(
                "El campo 'Header rule' debe ser una lista con al menos un elemento."
            )
        for item in self.header_rule:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "Cada elemento dentro de 'Header rule' debe ser una cadena no vacía."
                )

        if not isinstance(regla, dict):
            raise ValueError("El campo 'Regla' debe ser un objeto JSON.")

        def ensure_keys(expected_keys: set[str], allow_extra: bool = False) -> None:
            keys = set(regla.keys())
            if allow_extra:
                if not expected_keys.issubset(keys):
                    faltantes = expected_keys - keys
                    raise ValueError(
                        "La regla debe incluir las claves: " + ", ".join(sorted(faltantes))
                    )
                return
            if keys != expected_keys:
                raise ValueError(
                    "La regla debe contener exactamente las claves: " + ", ".join(sorted(expected_keys))
                )

        def ensure_int(name: str, minimum: int | None = None) -> None:
            value = regla.get(name)
            if not isinstance(value, int):
                raise ValueError(f"'{name}' debe ser un número entero.")
            if minimum is not None and value < minimum:
                raise ValueError(f"'{name}' debe ser mayor o igual a {minimum}.")

        def ensure_number(value: Any, allow_none: bool = False) -> None:
            if value is None and allow_none:
                return
            if not isinstance(value, (int, float)):
                raise ValueError("Los límites deben ser numéricos o nulos.")

        def ensure_non_empty_str(name: str) -> None:
            value = regla.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"'{name}' debe ser una cadena no vacía.")

        if tipo == TipoDatoEnum.TEXTO:
            ensure_keys({"Longitud mínima", "Longitud máxima"})
            ensure_int("Longitud mínima", minimum=0)
            ensure_int("Longitud máxima", minimum=0)

        elif tipo == TipoDatoEnum.NUMERO:
            ensure_keys({"Valor mínimo", "Valor máximo", "Número de decimales"})
            ensure_number(regla.get("Valor mínimo"), allow_none=True)
            ensure_number(regla.get("Valor máximo"), allow_none=True)
            ensure_int("Número de decimales", minimum=0)

        elif tipo == TipoDatoEnum.DOCUMENTO:
            ensure_keys({"Longitud mínima", "Longitud máxima"})
            ensure_int("Longitud mínima", minimum=1)
            ensure_int("Longitud máxima", minimum=1)

        elif tipo == TipoDatoEnum.LISTA:
            if not isinstance(regla, dict):  # pragma: no cover - defensive
                raise ValueError("La regla de tipo 'Lista' debe ser un objeto JSON.")

        elif tipo == TipoDatoEnum.LISTA_COMPLEJA:
            ensure_keys({"Lista compleja"})
            combinaciones = regla.get("Lista compleja")
            if not isinstance(combinaciones, list) or not combinaciones:
                raise ValueError(
                    "'Lista compleja' debe ser una lista con al menos una combinación permitida."
                )
            for combinacion in combinaciones:
                if not isinstance(combinacion, dict) or not combinacion:
                    raise ValueError(
                        "Cada combinación en 'Lista compleja' debe ser un objeto con al menos una clave."
                    )
                for campo, valor in combinacion.items():
                    if not isinstance(campo, str) or not campo.strip():
                        raise ValueError(
                            "Las claves de cada combinación deben ser cadenas no vacías."
                        )
                    if isinstance(valor, str):
                        if not valor.strip():
                            raise ValueError(
                                "Los valores de cada combinación deben ser cadenas o números no vacíos."
                            )
                        continue
                    if not isinstance(valor, (int, float)):
                        raise ValueError(
                            "Los valores de cada combinación deben ser cadenas o números no vacíos."
                        )
                    if isinstance(valor, float) and valor != valor:
                        raise ValueError(
                            "Los valores de cada combinación no pueden ser NaN."
                        )

        elif tipo == TipoDatoEnum.TELEFONO:
            ensure_keys({"Longitud mínima", "Código de país"})
            ensure_int("Longitud mínima", minimum=1)
            codigo = regla.get("Código de país")
            if not isinstance(codigo, str) or not re.fullmatch(r"^\+\d{1,3}$", codigo):
                raise ValueError("'Código de país' debe cumplir el patrón +<código numérico> de 1 a 3 dígitos.")

        elif tipo == TipoDatoEnum.CORREO:
            ensure_keys({"Formato", "Longitud máxima"})
            if not isinstance(regla.get("Formato"), str) or not regla["Formato"].strip():
                raise ValueError("'Formato' debe ser una cadena no vacía.")
            ensure_int("Longitud máxima", minimum=1)

        elif tipo == TipoDatoEnum.FECHA:
            ensure_keys({"Formato", "Fecha mínima", "Fecha máxima"})
            ensure_non_empty_str("Formato")
            ensure_non_empty_str("Fecha mínima")
            ensure_non_empty_str("Fecha máxima")
            formato = regla.get("Formato")
            formatos_validos = {"yyyy-MM-dd", "dd/MM/yyyy", "MM-dd-yyyy"}
            if formato not in formatos_validos:
                raise ValueError(
                    "El formato de fecha debe ser uno de: " + ", ".join(sorted(formatos_validos))
                )

        elif tipo == TipoDatoEnum.DEPENDENCIA:
            ensure_keys({"reglas especifica"})
            reglas_especifica = regla.get("reglas especifica")
            if not isinstance(reglas_especifica, list) or not reglas_especifica:
                raise ValueError("'reglas especifica' debe ser una lista con al menos un elemento.")

            allowed_types = set(DEPENDENCY_TYPE_HEADERS.keys())
            header_lookup = {_normalize_label(label): label for label in self.header}

            def ensure_dependency_list(label: str, values: list[Any]) -> None:
                if not values:
                    raise ValueError(
                        f"'{label}' debe ser una lista con al menos un elemento para la dependencia."
                    )

                def validate_leaf(value: Any) -> None:
                    if isinstance(value, str):
                        if not value.strip():
                            raise ValueError(
                                f"Cada valor dentro de '{label}' debe ser una cadena no vacía."
                            )
                        return
                    if isinstance(value, bool):
                        return
                    if isinstance(value, (int, float)):
                        if isinstance(value, float) and value != value:
                            raise ValueError(
                                f"Los valores numéricos dentro de '{label}' no pueden ser NaN."
                            )
                        return
                    raise ValueError(
                        f"Cada elemento dentro de '{label}' debe ser una cadena, número o booleano."
                    )

                for index, item in enumerate(values):
                    if isinstance(item, list):
                        if not item:
                            raise ValueError(
                                f"Los subarreglos en '{label}' deben contener al menos un elemento."
                            )
                        for nested in item:
                            validate_leaf(nested)
                        continue

                    validate_leaf(item)

            def remap_dependency_config(
                config: dict[str, Any], expected: tuple[str, ...], type_label: str
            ) -> dict[str, Any]:
                """Normaliza las claves de la configuración específica de dependencias."""

                normalized_expected = {_normalize_label(label): label for label in expected}
                remapped: dict[str, Any] = {}

                for raw_key, value in config.items():
                    if not isinstance(raw_key, str) or not raw_key.strip():
                        raise ValueError(
                            "Las claves dentro de la configuración dependiente deben ser cadenas no vacías."
                        )

                    normalized_key = _normalize_label(raw_key)
                    canonical_key = normalized_expected.get(normalized_key)
                    if canonical_key is None:
                        raise ValueError(
                            "La configuración para '"
                            + type_label
                            + "' solo puede incluir las claves: "
                            + ", ".join(sorted(expected))
                        )

                    if canonical_key in remapped:
                        raise ValueError(
                            "La configuración para '"
                            + type_label
                            + "' no puede repetir la clave '"
                            + canonical_key
                            + "'."
                        )

                    remapped[canonical_key] = value

                missing = [label for label in expected if label not in remapped]
                if missing:
                    raise ValueError(
                        "La configuración para '"
                        + type_label
                        + "' debe contener exactamente las claves: "
                        + ", ".join(sorted(expected))
                    )

                return remapped

            def ensure_config_list_values(
                values: Any, *, type_label: str, value_label: str
            ) -> None:
                if not isinstance(values, list) or not values:
                    raise ValueError(
                        value_label
                        + " en la configuración de '"
                        + type_label
                        + "' debe ser una lista con al menos un elemento."
                    )
                for elemento in values:
                    if not isinstance(elemento, str) or not elemento.strip():
                        raise ValueError(
                            "Cada valor dentro de "
                            + value_label
                            + " debe ser una cadena no vacía."
                        )

            def ensure_config_int_value(
                config: dict[str, Any], key: str, *, minimum: int | None, type_label: str
            ) -> None:
                value = config.get(key)
                if not isinstance(value, int):
                    raise ValueError(
                        f"'{key}' en la configuración de '{type_label}' debe ser un número entero."
                    )
                if minimum is not None and value < minimum:
                    raise ValueError(
                        f"'{key}' en la configuración de '{type_label}' debe ser mayor o igual a {minimum}."
                    )

            def ensure_config_numeric(
                value: Any, key: str, type_label: str, *, allow_none: bool = False
            ) -> None:
                if value is None and allow_none:
                    return
                if not isinstance(value, (int, float)):
                    raise ValueError(
                        f"'{key}' en la configuración de '{type_label}' debe ser numérico."
                    )

            dependent_label_reference: str | None = None
            normalized_dependent_reference: str | None = None
            expected_headers: set[str] = set()
            property_dependency_types = {
                "texto",
                "numero",
                "documento",
                "telefono",
                "correo",
                "fecha",
            }
            include_dependent_target_in_header = True

            for entrada in reglas_especifica:
                if not isinstance(entrada, dict) or len(entrada) < 2:
                    raise ValueError(
                        "Cada elemento de 'reglas especifica' debe ser un objeto con al menos dos claves."
                    )

                dependent_labels: list[str] = []
                type_label_seen: str | None = None
                has_supported_config = False

                for clave, contenido in entrada.items():
                    if not isinstance(clave, str) or not clave.strip():
                        raise ValueError(
                            "Cada clave dentro de 'reglas especifica' debe ser una cadena no vacía."
                        )
                    normalized_clave = _normalize_label(clave)

                    if isinstance(contenido, dict) and normalized_clave in allowed_types:
                        if type_label_seen is not None:
                            continue
                        type_label_seen = normalized_clave
                        has_supported_config = True
                        if normalized_clave == "texto":
                            contenido = remap_dependency_config(
                                contenido, ("Longitud mínima", "Longitud máxima"), clave
                            )
                            entrada[clave] = contenido
                            ensure_config_int_value(
                                contenido, "Longitud mínima", minimum=0, type_label=clave
                            )
                            ensure_config_int_value(
                                contenido, "Longitud máxima", minimum=0, type_label=clave
                            )
                        elif normalized_clave == "numero":
                            contenido = remap_dependency_config(
                                contenido,
                                ("Valor mínimo", "Valor máximo", "Número de decimales"),
                                clave,
                            )
                            entrada[clave] = contenido
                            ensure_config_numeric(
                                contenido.get("Valor mínimo"), "Valor mínimo", clave, allow_none=True
                            )
                            ensure_config_numeric(
                                contenido.get("Valor máximo"), "Valor máximo", clave, allow_none=True
                            )
                            ensure_config_int_value(
                                contenido, "Número de decimales", minimum=0, type_label=clave
                            )
                        elif normalized_clave == "documento":
                            contenido = remap_dependency_config(
                                contenido, ("Longitud mínima", "Longitud máxima"), clave
                            )
                            entrada[clave] = contenido
                            ensure_config_int_value(
                                contenido, "Longitud mínima", minimum=1, type_label=clave
                            )
                            ensure_config_int_value(
                                contenido, "Longitud máxima", minimum=1, type_label=clave
                            )
                        elif normalized_clave == "lista":
                            if not isinstance(contenido, dict):
                                raise ValueError(
                                    f"La configuración asociada a '{clave}' debe ser un objeto."
                                )

                            normalized_nested_keys = {
                                _normalize_label(nested_key): nested_key
                                for nested_key in contenido.keys()
                                if isinstance(nested_key, str)
                            }
                            canonical_list_key = normalized_nested_keys.get("lista")

                            if canonical_list_key is not None:
                                contenido = remap_dependency_config(
                                    contenido, ("Lista",), clave
                                )
                                entrada[clave] = contenido
                                valores = contenido.get("Lista")
                                ensure_config_list_values(
                                    valores, type_label=clave, value_label="'Lista'"
                                )
                            else:
                                if len(contenido) != 1:
                                    raise ValueError(
                                        "La configuración para '"
                                        + clave
                                        + "' debe definir exactamente un encabezado dependiente."
                                    )

                                nested_key, valores = next(iter(contenido.items()))
                                if not isinstance(nested_key, str) or not nested_key.strip():
                                    raise ValueError(
                                        "El encabezado definido dentro de '"
                                        + clave
                                        + "' debe ser una cadena no vacía."
                                    )
                                sanitized_label = nested_key.strip()
                                if sanitized_label != nested_key:
                                    contenido = {sanitized_label: valores}
                                    entrada[clave] = contenido
                                ensure_config_list_values(
                                    valores,
                                    type_label=clave,
                                    value_label="'" + sanitized_label + "'",
                                )
                        elif normalized_clave == "lista compleja":
                            contenido = remap_dependency_config(
                                contenido, ("Lista compleja",), clave
                            )
                            entrada[clave] = contenido
                            combinaciones = contenido.get("Lista compleja")
                            if not isinstance(combinaciones, list) or not combinaciones:
                                raise ValueError(
                                    "'Lista compleja' debe ser una lista con al menos una combinación permitida."
                                )
                            for combinacion in combinaciones:
                                if not isinstance(combinacion, dict) or not combinacion:
                                    raise ValueError(
                                        "Cada combinación en 'Lista compleja' debe ser un objeto con al menos una clave."
                                    )
                                for campo, valor in combinacion.items():
                                    if not isinstance(campo, str) or not campo.strip():
                                        raise ValueError(
                                            "Las claves de cada combinación deben ser cadenas no vacías."
                                        )
                                    if isinstance(valor, str):
                                        if not valor.strip():
                                            raise ValueError(
                                                "Los valores de cada combinación deben ser cadenas o números no vacíos."
                                            )
                                        continue
                                    if not isinstance(valor, (int, float)):
                                        raise ValueError(
                                            "Los valores de cada combinación deben ser cadenas o números no vacíos."
                                        )
                                    if isinstance(valor, float) and valor != valor:
                                        raise ValueError(
                                            "Los valores de cada combinación no pueden ser NaN."
                                        )
                        elif normalized_clave == "telefono":
                            contenido = remap_dependency_config(
                                contenido, ("Longitud mínima", "Código de país"), clave
                            )
                            entrada[clave] = contenido
                            ensure_config_int_value(
                                contenido, "Longitud mínima", minimum=1, type_label=clave
                            )
                            codigo = contenido.get("Código de país")
                            if not isinstance(codigo, str) or not re.fullmatch(r"^\+\d{1,3}$", codigo):
                                raise ValueError(
                                    "'Código de país' debe cumplir el patrón +<código numérico> de 1 a 3 dígitos."
                                )
                        elif normalized_clave == "correo":
                            contenido = remap_dependency_config(
                                contenido, ("Formato", "Longitud máxima"), clave
                            )
                            entrada[clave] = contenido
                            formato = contenido.get("Formato")
                            if not isinstance(formato, str) or not formato.strip():
                                raise ValueError("'Formato' debe ser una cadena no vacía.")
                            ensure_config_int_value(
                                contenido, "Longitud máxima", minimum=1, type_label=clave
                            )
                        elif normalized_clave == "fecha":
                            contenido = remap_dependency_config(
                                contenido, ("Formato", "Fecha mínima", "Fecha máxima"), clave
                            )
                            entrada[clave] = contenido
                            formato = contenido.get("Formato")
                            formatos_validos = {"yyyy-MM-dd", "dd/MM/yyyy", "MM-dd-yyyy"}
                            if formato not in formatos_validos:
                                raise ValueError(
                                    "El formato de fecha debe ser uno de: "
                                    + ", ".join(sorted(formatos_validos))
                                )
                            for etiqueta in ("Fecha mínima", "Fecha máxima"):
                                valor = contenido.get(etiqueta)
                                if not isinstance(valor, str) or not valor.strip():
                                    raise ValueError(
                                        f"'{etiqueta}' en la configuración de '{clave}' debe ser una cadena no vacía."
                                    )

                        for dependency_header in DEPENDENCY_TYPE_HEADERS[normalized_clave]:
                            canonical_header = header_lookup.get(
                                _normalize_label(dependency_header)
                            )
                            if canonical_header is not None:
                                expected_headers.add(canonical_header)
                        if normalized_clave in property_dependency_types:
                            include_dependent_target_in_header = False
                        continue

                    if normalized_clave in allowed_types:
                        raise ValueError(
                            "La configuración asociada a '"
                            + clave
                            + "' debe ser un objeto."
                        )

                    if isinstance(contenido, list):
                        ensure_dependency_list(clave, contenido)
                        canonical_header = header_lookup.get(normalized_clave, clave)
                        expected_headers.add(canonical_header)
                        has_supported_config = True
                        continue

                    if isinstance(contenido, dict):
                        raise ValueError(
                            "El valor asociado al campo dependiente no puede ser un objeto."
                        )
                    dependent_labels.append(clave)

                if not has_supported_config:
                    raise ValueError(
                        "Cada regla dependiente debe definir al menos una configuración soportada o una lista de valores permitidos."
                    )

                if not dependent_labels:
                    raise ValueError(
                        "Cada regla dependiente debe indicar el encabezado dependiente y su valor."
                    )

                if len(dependent_labels) > 1:
                    raise ValueError(
                        "Cada regla dependiente debe especificar un único campo dependiente."
                    )

                dependent_label = dependent_labels[0]
                normalized_dependent = _normalize_label(dependent_label)

                if dependent_label_reference is None:
                    dependent_label_reference = dependent_label
                    normalized_dependent_reference = normalized_dependent
                elif normalized_dependent_reference != normalized_dependent:
                    raise ValueError(
                        "Todas las reglas dependientes deben usar el mismo encabezado dependiente."
                    )

            if dependent_label_reference is None:
                raise ValueError(
                    "No se pudo identificar el campo dependiente dentro de 'reglas especifica'."
                )

            expected_headers.add(dependent_label_reference)
            dependent_target_label: str | None = None

            for candidate in self.header_rule:
                normalized_candidate = _normalize_label(candidate)
                if normalized_candidate == normalized_dependent_reference:
                    continue
                dependent_target_label = header_lookup.get(
                    normalized_candidate, candidate
                )
                break

            if dependent_target_label is None:
                for candidate in self.header:
                    normalized_candidate = _normalize_label(candidate)
                    if normalized_candidate == normalized_dependent_reference:
                        continue
                    dependent_target_label = candidate
                    break

            if dependent_target_label is None:
                dependent_target_label = dependent_label_reference

            if include_dependent_target_in_header:
                expected_headers.add(dependent_target_label)

            remapped_specifics: list[Any] = []
            normalized_target_label = _normalize_label(dependent_target_label)

            for entrada in reglas_especifica:
                if not isinstance(entrada, dict):
                    remapped_specifics.append(entrada)
                    continue

                transformed_entry = dict(entrada)
                for key, value in entrada.items():
                    if not isinstance(key, str):
                        continue

                    normalized_key = _normalize_label(key)
                    if normalized_key != "lista" or not isinstance(value, dict):
                        continue

                    normalized_nested_keys = {
                        _normalize_label(nested_key): nested_key
                        for nested_key in value.keys()
                        if isinstance(nested_key, str)
                    }
                    if normalized_target_label in normalized_nested_keys:
                        break

                    canonical_list_key = normalized_nested_keys.get("lista")
                    allowed_values = (
                        value.get(canonical_list_key)
                        if canonical_list_key is not None
                        else value.get("Lista")
                    )
                    if not isinstance(allowed_values, list):
                        continue

                    transformed_entry[key] = {
                        dependent_target_label: [deepcopy(item) for item in allowed_values]
                    }
                    break

                remapped_specifics.append(transformed_entry)

            self.regla = dict(self.regla)
            self.regla["reglas especifica"] = remapped_specifics
            normalized_header_values = {_normalize_label(item) for item in self.header}
            missing_headers = [
                label
                for label in expected_headers
                if _normalize_label(label) not in normalized_header_values
            ]
            if missing_headers:
                existing_headers = {
                    _normalize_label(label): label for label in self.header
                }
                for label in sorted(missing_headers):
                    normalized_label = _normalize_label(label)
                    if normalized_label in existing_headers:
                        continue
                    self.header.append(label)
                    existing_headers[normalized_label] = label
                header_lookup.update(existing_headers)

        elif tipo == TipoDatoEnum.DUPLICADOS:
            candidate_keys = ("Campos", "Columnas", "Fields", "fields")
            extracted_fields: list[str] = []

            for key in candidate_keys:
                raw_fields = regla.get(key)
                if raw_fields is None:
                    continue
                if not isinstance(raw_fields, list) or not raw_fields:
                    raise ValueError(
                        f"'{key}' debe ser una lista con al menos un elemento."
                    )

                normalized_fields: list[str] = []
                for field in raw_fields:
                    if not isinstance(field, str) or not field.strip():
                        raise ValueError(
                            "Cada elemento definido en la lista de campos debe ser una cadena no vacía."
                        )
                    normalized_fields.append(field.strip())

                if normalized_fields:
                    extracted_fields.extend(normalized_fields)
                    break

            if not extracted_fields:
                raise ValueError(
                    "La configuración para la regla de duplicados debe incluir al menos un listado de campos."
                )

            allowed_booleans = (
                "Ignorar vacios",
                "Ignorar vacíos",
                "Ignorar vacias",
                "Ignorar vacías",
                "Ignore empty",
                "Ignore empties",
            )
            for flag in allowed_booleans:
                value = regla.get(flag)
                if value is None:
                    continue
                if not isinstance(value, bool):
                    raise ValueError(f"'{flag}' debe ser un valor booleano.")

        elif tipo == TipoDatoEnum.VALIDACION_CONJUNTA:
            ensure_keys({"Nombre de campos"})
            nombres = regla.get("Nombre de campos")
            if not isinstance(nombres, list) or not nombres:
                raise ValueError("'Nombre de campos' debe ser una lista con al menos un elemento.")
            for nombre in nombres:
                if not isinstance(nombre, str) or not nombre.strip():
                    raise ValueError("Cada nombre de campo debe ser una cadena no vacía.")

        else:  # pragma: no cover - defensive fallback
            raise ValueError("Tipo de dato no soportado para la validación de la regla.")

        return self


def _normalize_label(label: str) -> str:
    normalized = unicodedata.normalize("NFKD", label)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower().strip()
