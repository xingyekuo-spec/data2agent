import { baseFixture, type ScenarioFixture } from './base'
import { mappingPreviewDraftInvalid } from './mapping-preview'

/** Preview 422:草稿语义非法(draft_invalid)。 */
export const previewDraftInvalidFixture = {
  ...baseFixture,
  mappingPreviewStatus: 422,
  mappingPreviewError: mappingPreviewDraftInvalid,
} satisfies ScenarioFixture
