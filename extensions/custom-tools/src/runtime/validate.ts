/**
 * Minimal JSON-Schema-shaped validator for tool argument checking.
 *
 * Generated tool source (and hand-written tools that want it) gets a
 * `validate(value, schema, path?)` global. Returns null on success, throws
 * a clear error on mismatch.
 *
 * Scope: subset of JSON Schema that covers what wizard-imported tools need.
 *   ✓ type: string | number | integer | boolean | object | array | null
 *   ✓ object.properties + object.required
 *   ✓ array.items (single schema, not tuple)
 *   ✓ enum
 *   ✗ oneOf / allOf / anyOf — not needed yet
 *   ✗ format — not needed yet (string is just string)
 *   ✗ $ref — generators resolve refs before emitting
 *
 * Validation failures throw — they're never silently swallowed. The tool
 * source can catch them like any other error if it wants to handle
 * gracefully.
 */

interface JsonSchemaLike {
  type?: string;
  properties?: Record<string, JsonSchemaLike>;
  required?: string[];
  items?: JsonSchemaLike;
  enum?: unknown[];
}

export function validate(
  value: unknown,
  schema: JsonSchemaLike | undefined,
  path: string = "",
): void {
  if (!schema || typeof schema !== "object") return;

  const label = path || "value";

  if (Array.isArray(schema.enum)) {
    if (!schema.enum.includes(value)) {
      throw new Error(`${label}: must be one of ${JSON.stringify(schema.enum)}`);
    }
  }

  if (typeof schema.type === "string") {
    if (!matchesType(value, schema.type)) {
      throw new Error(
        `${label}: expected ${schema.type}, got ${describeType(value)}`,
      );
    }
  }

  if (schema.type === "object" && isPlainObject(value)) {
    const obj = value as Record<string, unknown>;
    const required = Array.isArray(schema.required) ? schema.required : [];
    for (const req of required) {
      if (!(req in obj)) {
        throw new Error(`${label}: missing required property '${req}'`);
      }
    }
    if (schema.properties) {
      for (const [key, propSchema] of Object.entries(schema.properties)) {
        if (key in obj) {
          validate(obj[key], propSchema, path ? `${path}.${key}` : key);
        }
      }
    }
  }

  if (schema.type === "array" && Array.isArray(value) && schema.items) {
    value.forEach((item, i) => {
      validate(item, schema.items, `${label}[${i}]`);
    });
  }
}

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "string":  return typeof value === "string";
    case "number":  return typeof value === "number" && Number.isFinite(value);
    case "integer": return Number.isInteger(value);
    case "boolean": return typeof value === "boolean";
    case "object":  return isPlainObject(value);
    case "array":   return Array.isArray(value);
    case "null":    return value === null;
    default:        return true; // unknown type — be permissive
  }
}

function isPlainObject(v: unknown): boolean {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function describeType(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}
