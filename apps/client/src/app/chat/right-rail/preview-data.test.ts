import { describe, expect, it } from 'vitest'

import { parseDelimited, parseJsonValue, parseTomlValue, parseYamlValue } from './preview-data'

describe('parseJsonValue', () => {
  it('parses valid json', () => {
    expect(parseJsonValue('{"a":[1,2]}')).toEqual({ value: { a: [1, 2] } })
  })

  it('returns null for a partially written file', () => {
    expect(parseJsonValue('{"a": [1,')).toBeNull()
  })
})

describe('parseDelimited', () => {
  it('splits rows and columns', () => {
    expect(parseDelimited('a,b\n1,2\n', ',')).toEqual([
      ['a', 'b'],
      ['1', '2']
    ])
  })

  it('honors quoted fields with delimiters, newlines and escaped quotes', () => {
    expect(parseDelimited('name,note\n"Smith, J","said ""hi""\nagain"', ',')).toEqual([
      ['name', 'note'],
      ['Smith, J', 'said "hi"\nagain']
    ])
  })

  it('handles tabs and trailing CR', () => {
    expect(parseDelimited('a\tb\r\n1\t2', '\t')).toEqual([
      ['a', 'b'],
      ['1', '2']
    ])
  })

  it('keeps empty trailing fields', () => {
    expect(parseDelimited('a,b,c\n1,,3', ',')).toEqual([
      ['a', 'b', 'c'],
      ['1', '', '3']
    ])
  })
})

describe('parseYamlValue', () => {
  it('parses a mapping', () => {
    expect(parseYamlValue('name: demo\nports:\n  - 80\n  - 443\n')).toEqual({
      value: { name: 'demo', ports: [80, 443] }
    })
  })

  it('returns a list for a multi-document stream and the bare value for one document', () => {
    expect(parseYamlValue('a: 1\n---\nb: 2\n')).toEqual({ value: [{ a: 1 }, { b: 2 }] })
    expect(parseYamlValue('a: 1\n')).toEqual({ value: { a: 1 } })
  })

  it('normalizes timestamps to iso strings', () => {
    expect(parseYamlValue('when: 2020-01-02T03:04:05Z\n')).toEqual({ value: { when: '2020-01-02T03:04:05.000Z' } })
  })

  it('breaks anchor cycles instead of recursing forever', () => {
    const parsed = parseYamlValue('a: &x\n  self: *x\n')

    expect(parsed).toEqual({ value: { a: { self: '[circular]' } } })
  })

  it('returns null for invalid or empty documents', () => {
    expect(parseYamlValue('a:\n- b\n  c: [')).toBeNull()
    expect(parseYamlValue('# just a comment\n')).toBeNull()
  })
})

describe('parseTomlValue', () => {
  it('parses tables and arrays of tables', () => {
    expect(parseTomlValue('title = "demo"\n\n[[bin]]\nname = "a"\n\n[[bin]]\nname = "b"\n')).toEqual({
      value: { bin: [{ name: 'a' }, { name: 'b' }], title: 'demo' }
    })
  })

  it('normalizes datetimes to iso strings', () => {
    const parsed = parseTomlValue('when = 1979-05-27T07:32:00Z\ncount = 42\n')

    expect(parsed?.value).toEqual({ count: 42, when: '1979-05-27T07:32:00.000Z' })
  })

  it('returns null for invalid toml, including integers it cannot represent', () => {
    expect(parseTomlValue('a = = 1')).toBeNull()
    expect(parseTomlValue('big = 9223372036854775807\n')).toBeNull()
  })
})

describe('tree node budget', () => {
  it('gives up on an alias bomb instead of materializing it', () => {
    // Classic "billion laughs": each level repeats the one below nine times, so
    // a few hundred bytes would expand into millions of nodes.
    const levels = ['a: &a ["x","x","x","x","x","x","x","x","x"]']

    for (let level = 1; level < 8; level += 1) {
      const alias = `*${String.fromCharCode(96 + level)}`
      const anchor = String.fromCharCode(97 + level)

      levels.push(`${anchor}: &${anchor} [${Array(9).fill(alias).join(',')}]`)
    }

    const start = Date.now()

    expect(parseYamlValue(levels.join('\n'))).toBeNull()
    expect(Date.now() - start).toBeLessThan(5000)
  })
})
