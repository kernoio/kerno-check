import {ctx} from '@kerno/ts-sandbox'
import {expect} from 'vitest'

export default function happy_path() {
  ctx.step('GET / returns 200 and status ok', async () => {
    const res = await fetch(`${process.env.SUT_BASE_URL}/`)
    expect(res.status).toBe(200)
    const body = await res.text()
    expect(JSON.parse(body).status).toBe('ok')
  })
}
