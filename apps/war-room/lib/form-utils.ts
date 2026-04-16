export function splitCommaSeparatedValues(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
}

export function stringifyJsonValue(value: unknown): string {
  if (value == null) {
    return ''
  }

  return JSON.stringify(value, null, 2)
}

function parseJsonRecord(
  rawValue: string,
  fieldName: string,
  allowBlank: boolean
): { error?: string; value?: Record<string, unknown> } {
  const trimmed = rawValue.trim()

  if (trimmed.length === 0) {
    return allowBlank ? {} : { error: `${fieldName} must be a JSON object.` }
  }

  try {
    const parsed: unknown = JSON.parse(trimmed)

    if (parsed == null || Array.isArray(parsed) || typeof parsed !== 'object') {
      return { error: `${fieldName} must be a JSON object.` }
    }

    return { value: parsed as Record<string, unknown> }
  } catch {
    return { error: `${fieldName} must be valid JSON.` }
  }
}

export function parseOptionalJsonRecord(
  rawValue: string,
  fieldName: string
): { error?: string; value?: Record<string, unknown> } {
  return parseJsonRecord(rawValue, fieldName, true)
}

export function parseRequiredJsonRecord(
  rawValue: string,
  fieldName: string
): { error?: string; value?: Record<string, unknown> } {
  return parseJsonRecord(rawValue, fieldName, false)
}

export function trimToUndefined(value: string): string | undefined {
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}
