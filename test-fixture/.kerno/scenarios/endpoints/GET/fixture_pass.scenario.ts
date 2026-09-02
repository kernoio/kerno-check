import {ctx} from '@kerno/ts-sandbox'
import {expect} from 'vitest'

export const meta = {
  description: 'Passes against the fixture SUT. Proves the action reports a satisfied assertion.',
  path: 'GET /',
}

export default function fixture_pass() {
  let status: number
  let body: string

  ctx.act('GET / on the system under test', async () => {
    const res = await fetch(`${process.env.SUT_BASE_URL}/`)
    status = res.status
    body = await res.text()
  })

  ctx.assert('returns 200 and status ok', () => {
    expect(status).toBe(200)
    expect(JSON.parse(body).status).toBe('ok')
  })
}
