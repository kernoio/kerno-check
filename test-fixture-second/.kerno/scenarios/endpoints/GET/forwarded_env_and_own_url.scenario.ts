import {ctx} from '@kerno/ts-sandbox'
import {expect} from 'vitest'

export const meta = {
  description:
    'Proves the two things the apps and forward-env inputs exist for: this app was replayed ' +
    'against ITS OWN sut-url rather than the first app\'s, and a variable named in forward-env ' +
    'reached the scenario.',
  path: 'GET /',
}

export default function forwarded_env_and_own_url() {
  let status: number
  let sutBaseUrl: string
  let forwarded: string | undefined

  ctx.act('GET / on this app\'s own system under test', async () => {
    sutBaseUrl = process.env.SUT_BASE_URL ?? ''
    forwarded = process.env.KERNO_SELF_TEST_TOKEN
    const res = await fetch(`${sutBaseUrl}/`)
    status = res.status
  })

  ctx.assert('replayed against this app\'s own URL, with the forwarded variable present', () => {
    expect(status, 'the SUT answered').toBe(200)

    // 8081 is THIS app's port. If the action ignored the per-app mapping and fell back to a single
    // sut-url, this would be 8080 and the assertion is the only thing that would notice.
    expect(sutBaseUrl, `SUT_BASE_URL was ${sutBaseUrl}`).toContain('8081')

    // Without forward-env this is undefined, and no HTTP assertion above would have failed —
    // which is exactly the silent partial pass the input exists to prevent.
    expect(forwarded, 'KERNO_SELF_TEST_TOKEN was not forwarded to the scenario').toBe(
      'forwarded-ok',
    )
  })
}
