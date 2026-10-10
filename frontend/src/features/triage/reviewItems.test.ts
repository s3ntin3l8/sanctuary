import { expect, test } from 'vitest'

import { documentReview } from '../../test/fixtures'
import { reviewReasonLabel } from '../documents/reviewReasons'
import { reviewItems } from './ReviewChecklist'

test('a date that looks wrong sends the user to the issued-date field', () => {
  const items = reviewItems({
    ...documentReview,
    review_reasons: ['issued_date_suspect'],
    metadata: [],
  })
  expect(items.map((i) => i.label)).toContain('Check metadata: issued date')
  expect(reviewReasonLabel('issued_date_suspect')).toBe('issued date looks wrong')
})

test('a conflicting Aktenzeichen sends the user to the metadata', () => {
  const items = reviewItems({ ...documentReview, review_reasons: ['az_conflict'], metadata: [] })
  expect(items.map((i) => i.label)).toContain('Check metadata: Aktenzeichen')
  expect(reviewReasonLabel('az_conflict')).toBe('Aktenzeichen differs within bundle — check')
})
