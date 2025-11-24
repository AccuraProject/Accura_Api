# Documentación del motor de reglas asistido por IA

## 1. Visión general
El asistente inteligente expuesto por el endpoint `POST /assistant/analyze` recibe mensajes en lenguaje natural y devuelve definiciones de reglas de validación estructuradas. El flujo combina contexto del dominio (reglas ya registradas) con un esquema JSON estricto para garantizar respuestas consistentes antes de que una regla se guarde en plantillas o catálogos internos.【F:app/interfaces/api/routes/assistant.py†L304-L329】

La interacción con el modelo de lenguaje se encapsula en `StructuredChatService`, que prepara el prompt, aplica restricciones de formato y valida exhaustivamente la respuesta antes de exponerla al resto del sistema.【F:app/infrastructure/openai_client.py†L577-L935】

## 2. Componentes principales
- **API del asistente (`app/interfaces/api/routes/assistant.py`)**: Orquesta la llamada al servicio de IA, aporta reglas recientes como contexto y homologa la respuesta contra el modelo `AssistantMessageResponse` de FastAPI.【F:app/interfaces/api/routes/assistant.py†L304-L353】
- **Cliente de OpenAI (`app/infrastructure/openai_client.py`)**: Implementa heurísticas para filtrar mensajes fuera de contexto, construye el prompt estructurado, solicita una respuesta compatible con JSON Schema y aplica validaciones adicionales específicas por tipo de dato.【F:app/infrastructure/openai_client.py†L445-L935】
- **Esquema JSON de referencia (`app/schemas/regla_de_campo.schema.json`)**: Define los campos obligatorios y las restricciones específicas por tipo de regla para validar tanto la respuesta del modelo como la información almacenada posteriormente.【F:app/schemas/regla_de_campo.schema.json†L1-L418】

## 3. Flujo de solicitud en el endpoint
1. **Normalización de contexto**. El endpoint carga hasta 15 reglas recientes (5 de cualquier tipo y 10 listas) y genera un catálogo agrupado por tipo. Este material sirve como “memoria corta” para el modelo, reduciendo duplicados y manteniendo consistencia terminológica.【F:app/interfaces/api/routes/assistant.py†L314-L328】【F:app/interfaces/api/routes/assistant.py†L256-L283】
2. **Invocación al servicio**. Se delega en `StructuredChatService.generate_structured_response`, pasando el mensaje original y el catálogo serializado. Cualquier excepción se traduce en códigos HTTP específicos (400 para mensajes fuera de tema y 502 para problemas con la API).【F:app/interfaces/api/routes/assistant.py†L304-L339】
3. **Validación de respuesta**. La estructura devuelta se parsea con Pydantic (`AssistantMessageResponse`), lo que añade una última capa de seguridad contra cambios inesperados en el esquema.【F:app/interfaces/api/routes/assistant.py†L341-L353】

## 4. Preprocesamiento y salvaguardas antes de llamar a OpenAI
`StructuredChatService` valida la configuración (clave API, modelo, compatibilidad con `response_format`) y prepara el cliente Responses del SDK 1.x.【F:app/infrastructure/openai_client.py†L580-L626】

Antes de emitir una llamada al modelo:
- Se comprueba que el mensaje realmente trata sobre validaciones mediante palabras clave y detección de patrones (longitud, formato, catálogos). Si no es relevante, se responde con un error específico para el usuario.【F:app/infrastructure/openai_client.py†L445-L509】
- Se detectan solicitudes masivas de catálogos (por ejemplo, “lista de todos los departamentos”) para ajustar las instrucciones del prompt y limitar la respuesta.【F:app/infrastructure/openai_client.py†L85-L111】【F:app/infrastructure/openai_client.py†L646-L655】【F:app/infrastructure/openai_client.py†L707-L724】
- Se habilita un modo de reintento cuando la primera llamada devuelve errores típicos (JSON inválido, ausencia de campos obligatorios). En el segundo intento se trunca el mensaje y se añaden instrucciones para priorizar la información esencial.【F:app/infrastructure/openai_client.py†L529-L563】【F:app/infrastructure/openai_client.py†L644-L721】

## 5. Construcción del prompt y uso de contexto
El prompt consta de:
- **System prompt**: obliga al modelo a responder exclusivamente con JSON válido.【F:app/infrastructure/openai_client.py†L675-L678】
- **Instrucciones de tarea**: describen el dominio InsurTech, enumeran los campos obligatorios y dan reglas específicas para dependencias y listas complejas, dejando claro que el modelo no debe inventar el `Header` cuando esas secciones pueden reconstruirse desde `Regla`.【F:app/infrastructure/openai_client.py†L680-L707】
- **Ajustes dinámicos**: si el mensaje fue truncado o pide catálogos extensos, se añaden instrucciones complementarias que limitan el alcance y piden explicaciones adicionales.【F:app/infrastructure/openai_client.py†L707-L724】
- **Contexto reciente**: cuando hay reglas recientes, se inyectan como mensajes adicionales para que el modelo respete terminología y evite duplicados.【F:app/infrastructure/openai_client.py†L722-L790】
- **Schema enforcement**: cuando el SDK lo permite se pasa `response_format` con el JSON Schema; de lo contrario se incluye el schema en texto para que el modelo cumpla las restricciones manualmente.【F:app/infrastructure/openai_client.py†L731-L767】

## 6. Postprocesado y validaciones adicionales
Tras obtener la respuesta:
1. **Normalización de JSON**. Se eliminan fences de Markdown y comas residuales antes de decodificar.【F:app/infrastructure/openai_client.py†L806-L821】
2. **Campos obligatorios**. Se verifica que existan los nueve campos principales y que textos críticos no estén vacíos.【F:app/infrastructure/openai_client.py†L825-L866】
3. **Tipificación**. Solo se aceptan los tipos enumerados (`Texto`, `Número`, `Dependencia`, etc.) definidos en el schema.【F:app/infrastructure/openai_client.py†L844-L858】【F:app/schemas/regla_de_campo.schema.json†L19-L33】
4. **Headers**. Para cada tipo simple se forzan encabezados estándar (por ejemplo, `Longitud mínima` / `Longitud máxima` en texto). En reglas de lista compleja se reconstruyen las columnas a partir del primer bloque de combinaciones y en dependencias se infieren hojas finales para asegurar que el header coincide con las claves configurables reales.【F:app/infrastructure/openai_client.py†L867-L905】【F:app/infrastructure/openai_client.py†L873-L884】
5. **Header rule**. Si el modelo no la genera, se reconstruye en función del tipo (lista compleja, dependencia, validación conjunta, duplicados) y se valida que contenga al menos un elemento.【F:app/infrastructure/openai_client.py†L910-L928】
6. **Regla**. Se garantiza que el bloque `Regla` sea un objeto JSON; el contenido específico se valida posteriormente contra el schema cuando la regla se persiste.【F:app/infrastructure/openai_client.py†L930-L935】【F:app/schemas/regla_de_campo.schema.json†L50-L418】

## 7. Manejo particular de reglas de dependencia
Las dependencias requieren analizar `reglas especifica` para detectar:
- **Etiquetas condicionantes vs. dependientes**, usando alias de tipo (`Texto`, `Lista`, etc.) para separar configuraciones anidadas.【F:app/infrastructure/openai_client.py†L191-L405】
- **Hojas finales** que se usarán como encabezados, descartando duplicados y normalizando acentos o mayúsculas.【F:app/infrastructure/openai_client.py†L191-L405】【F:app/infrastructure/openai_client.py†L867-L905】
- **Construcción del Header** respetando `Header rule`: si la regla dependiente solo lista catálogos simples, el encabezado replica la pareja condicionante/dependiente; cuando el dependiente tiene restricciones internas (longitudes, formatos, etc.), el header combina el campo condicionante con los nombres de esas propiedades y omite el nombre de la columna dependiente.【F:app/infrastructure/openai_client.py†L287-L366】【F:app/infrastructure/openai_client.py†L848-L906】
- **Remapeo de catálogos** cuando la dependencia incluye listas internas, asegurando que el valor dependiente se vincula al header correcto en el resumen que ve el usuario.【F:app/interfaces/api/routes/assistant.py†L112-L253】

## 8. Integración con plantillas y persistencia
Cuando la regla aceptada se asocia a una columna de plantilla, se aplican validaciones adicionales:
- Cada columna debe tener al menos una regla y los encabezados declarados deben ser coherentes con el tipo de columna.【F:app/application/use_cases/template_columns/create_template_column.py†L104-L171】
- La función `ensure_rule_header_dependencies` comprueba que las dependencias declaradas por las reglas existen en la plantilla y que no faltan columnas requeridas, evitando inconsistencias al guardar el esquema final.【F:app/application/use_cases/template_columns/create_template_column.py†L132-L182】【F:app/application/use_cases/template_columns/validators.py†L1-L170】【F:app/application/use_cases/template_columns/validators.py†L533-L586】

## 9. Gestión de errores y retroalimentación al usuario
- **Mensajes fuera de tema**: se devuelven con un detalle explícito que explica por qué la solicitud no se relaciona con validaciones.【F:app/infrastructure/openai_client.py†L445-L509】
- **Fallos en OpenAI**: cualquier error de red o de formato se encapsula en `OpenAIServiceError` y se transforma en un `502 Bad Gateway`. El servicio solo reintenta cuando detecta errores recuperables.【F:app/infrastructure/openai_client.py†L513-L661】【F:app/interfaces/api/routes/assistant.py†L330-L339】
- **Desalineaciones con el schema**: si la respuesta final no coincide con Pydantic, se loguea el payload crudo para auditoría y se devuelve un error de integración, permitiendo identificar rápidamente cambios en el modelo o en el schema.【F:app/interfaces/api/routes/assistant.py†L341-L353】

## 10. Resumen operativo
El motor asistido por IA opera como un pipeline estrictamente controlado: filtra mensajes para garantizar relevancia, aporta contexto corporativo, fuerza el cumplimiento de un JSON Schema y aplica normalizaciones adicionales específicas por tipo de dato. Con ello se asegura que cada regla generada sea coherente, completa y lista para integrarse en los flujos de validación de datos del dominio asegurador.
