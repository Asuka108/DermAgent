import { z, type ZodTypeAny } from "zod";

export function asObject(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function enumSchema(values: unknown[]): ZodTypeAny {
  const filtered = values.filter((v): v is string | number => typeof v === "string" || typeof v === "number");
  if (!filtered.length) return z.any();
  if (filtered.every((v) => typeof v === "string")) {
    const uniq = [...new Set(filtered)];
    return z.enum(uniq as [string, ...string[]]);
  }
  const literals = filtered.map((v) => z.literal(v));
  if (literals.length === 1) return literals[0]!;
  return z.union(literals as unknown as [ZodTypeAny, ZodTypeAny, ...ZodTypeAny[]]);
}

function fromJsonSchema(schema: unknown): ZodTypeAny {
  const obj = asObject(schema);
  if (!obj) return z.any();

  if (Array.isArray(obj.enum)) return enumSchema(obj.enum);

  const type = typeof obj.type === "string" ? obj.type : undefined;
  switch (type) {
    case "string":
      return z.string();
    case "number":
      return z.number();
    case "integer":
      return z.number().int();
    case "boolean":
      return z.boolean();
    case "array": {
      const itemSchema = fromJsonSchema(obj.items);
      return z.array(itemSchema);
    }
    case "object": {
      const propertiesObj = asObject(obj.properties) ?? {};
      const requiredSet = new Set(Array.isArray(obj.required) ? obj.required.filter((x): x is string => typeof x === "string") : []);
      const shape: Record<string, ZodTypeAny> = {};
      for (const [key, value] of Object.entries(propertiesObj)) {
        const field = fromJsonSchema(value);
        shape[key] = requiredSet.has(key) ? field : field.optional();
      }
      return z.object(shape).passthrough();
    }
    default:
      if (obj.properties || obj.required) {
        return fromJsonSchema({ ...obj, type: "object" });
      }
      return z.any();
  }
}

export function schemaToZodInput(schema: unknown): ZodTypeAny {
  const obj = asObject(schema);
  if (!obj) return z.object({}).passthrough();
  const jsonSchema = asObject(obj.inputSchema) ?? asObject(obj.input) ?? obj;
  const parsed = fromJsonSchema(jsonSchema);
  if (parsed instanceof z.ZodObject) return parsed.passthrough();
  return z.object({ payload: parsed }).passthrough();
}
