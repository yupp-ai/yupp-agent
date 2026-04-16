import { describe, expect, it } from 'bun:test'
import {
  parseOptionalJsonRecord,
  parseRequiredJsonRecord,
  splitCommaSeparatedValues,
} from './form-utils'

describe('form-utils', () => {
  it('splits comma separated values and removes blanks', () => {
    expect(splitCommaSeparatedValues('alpha, beta, , gamma')).toEqual([
      'alpha',
      'beta',
      'gamma',
    ])
  })

  it('accepts blank optional JSON', () => {
    expect(parseOptionalJsonRecord('   ', 'Context')).toEqual({})
  })

  it('rejects blank required JSON', () => {
    expect(parseRequiredJsonRecord('', 'Tool permissions').error).toBe(
      'Tool permissions must be a JSON object.'
    )
  })

  it('rejects non-object JSON', () => {
    expect(parseOptionalJsonRecord('[]', 'Context').error).toBe(
      'Context must be a JSON object.'
    )
  })

  it('parses JSON objects', () => {
    expect(
      parseRequiredJsonRecord('{"*":"allow"}', 'Tool permissions')
    ).toEqual({
      value: {
        '*': 'allow',
      },
    })
  })
})
