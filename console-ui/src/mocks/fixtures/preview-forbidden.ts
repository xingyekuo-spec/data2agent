import { baseFixture, type ScenarioFixture } from './base'
import { mappingPreviewForbidden } from './mapping-preview'

/** Preview 403:控制台未配置 Token(token_not_configured)。 */
export const previewForbiddenFixture = {
  ...baseFixture,
  mappingPreviewStatus: 403,
  mappingPreviewError: mappingPreviewForbidden,
} satisfies ScenarioFixture
