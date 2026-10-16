import unittest
from unittest.mock import MagicMock, patch
from automation.visual_listing_reader import parse_ocr_fields, extract_video_sources, read_listing_edit_visual, merge_control_fields


class VisualReaderTests(unittest.TestCase):
    def test_internal_numeric_model_id_never_replaces_real_model(self):
        self.assertNotIn('model', parse_ocr_fields([{'text': 'Model: 985374995825728', 'confidence': 99}]))
        fields = {'model': {'value': 'RC 350', 'state': 'needs_review'}}
        merge_control_fields(fields, {'model': {'value': '985374995825728', 'state': 'read'}}, 'form.png')
        self.assertEqual(fields['model']['value'], 'RC 350')

    def test_fallback_can_read_screenshot_without_successful_dom_readiness(self):
        import tempfile
        driver = MagicMock(current_url='https://www.facebook.com/marketplace/edit/?listing_id=123')
        driver.execute_script.side_effect = [object(), 0, None, None, {}, [], [0, 0, True], None]
        driver.save_screenshot.return_value = True
        with tempfile.TemporaryDirectory() as folder, \
             patch('automation.visual_listing_reader.time.sleep'), \
             patch('automation.visual_listing_reader.wait_for_edit_form_ready') as readiness:
            result = read_listing_edit_visual(driver, folder, allow_unstructured=True,
                ocr=lambda path: [{'text': 'Title: Project car', 'confidence': 95},
                                  {'text': 'Price: $9800', 'confidence': 95}])
        readiness.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertEqual(result['posts'][0]['title'], 'Project car')
        self.assertTrue(result['requires_review'])

    def test_observed_label_errors_and_location_icon(self):
        result = parse_ocr_fields([
            {'text': text, 'confidence': 95, 'top': top, 'left': left}
            for text, top, left in [('ehicle type', 10, 34), ('Car/van', 28, 34),
                ('Mode!', 100, 34), ('transit van', 118, 34),
                ('Location', 200, 70), ('9', 206, 36), ('Islamabad', 218, 70)]])
        self.assertEqual(result['vehicle_type']['value'], 'Car/van')
        self.assertEqual(result['model']['value'], 'transit van')
        self.assertEqual(result['location']['value'], 'Islamabad')

    def test_helper_text_is_not_description_or_checkbox_state(self):
        result = parse_ocr_fields([{'text': text, 'confidence': 95} for text in
            ['Description', "Tell buyers anything that you haven't had the",
             'This vehicle has a clean title.', 'This vehicle has no significant damage']])
        self.assertEqual(result, {})

    def test_controls_resolve_ocr_errors_and_preserve_empty_state(self):
        fields = {'price': {'value': 'MADS567,000', 'state': 'needs_review'}}
        merge_control_fields(fields, {
            'price': {'value': 'MAD567,000', 'state': 'read'},
            'description': {'value': '', 'state': 'empty'},
            'clean_title': {'value': 'Yes', 'state': 'read'},
            'exterior_color': {'value': '', 'state': 'unavailable'}}, 'form.png')
        self.assertEqual(fields['price']['value'], 'MAD567,000')
        self.assertTrue(fields['price']['ocr_disagreement'])
        self.assertEqual(fields['description']['state'], 'empty')
        self.assertEqual(fields['clean_title']['value'], 'Yes')
        self.assertNotIn('exterior_color', fields)
    def test_case_insensitive_inline_and_following_values(self):
        result = parse_ocr_fields([
            {'text': 'YEAR: 2020', 'confidence': 95},
            {'text': 'Exterior colour', 'confidence': 90},
            {'text': 'Blue', 'confidence': 85},
        ])
        self.assertEqual(result['year']['value'], '2020')
        self.assertEqual(result['exterior_color']['value'], 'Blue')
        self.assertEqual(result['exterior_color']['confidence'], 85)
        self.assertEqual(result['year']['state'], 'needs_review')

    def test_next_label_and_placeholder_are_not_values(self):
        result = parse_ocr_fields([{'text': text, 'confidence': 95} for text in
                                  ['Year', 'Make', 'Select make', 'Mileage: unknown']])
        self.assertNotIn('year', result)
        self.assertNotIn('make', result)
        self.assertNotIn('mileage', result)

    def test_video_sources_deduplicate_and_report_blobs(self):
        driver = MagicMock()
        driver.execute_script.return_value = ['https://video.fbcdn.net/movie.mp4'] * 2 + ['blob:https://www.facebook.com/id']
        direct, unresolved = extract_video_sources(driver)
        self.assertEqual(direct, ['https://video.fbcdn.net/movie.mp4'])
        self.assertEqual(len(unresolved), 1)

    def test_preview_and_numeric_location_are_rejected(self):
        result = parse_ocr_fields([{'text': text, 'confidence': 95} for text in
                                  ['Description will appear here.', 'Location', '9']])
        self.assertEqual(result, {})

    def test_value_from_other_column_is_rejected(self):
        result = parse_ocr_fields([
            {'text': 'Make', 'confidence': 95, 'top': 100, 'left': 20},
            {'text': 'Preview', 'confidence': 95, 'top': 110, 'left': 650},
        ])
        self.assertEqual(result, {})

    def test_loading_form_does_not_capture(self):
        import tempfile
        driver = MagicMock(current_url='https://www.facebook.com/marketplace/edit/?listing_id=123')
        with tempfile.TemporaryDirectory() as folder, patch(
                'automation.visual_listing_reader.wait_for_edit_form_ready',
                side_effect=RuntimeError('Form still loading')):
            with self.assertRaisesRegex(RuntimeError, 'still loading'):
                read_listing_edit_visual(driver, folder)
        driver.save_screenshot.assert_not_called()

    def test_empty_ocr_is_reported_as_failure(self):
        import tempfile
        driver = MagicMock(current_url='https://www.facebook.com/marketplace/edit/?listing_id=123')
        scroll = object()
        driver.execute_script.side_effect = [scroll, 0, None, None, {}, [], [0, 0, True], None]
        driver.save_screenshot.return_value = True
        with tempfile.TemporaryDirectory() as folder, patch('automation.visual_listing_reader.time.sleep'), patch('automation.visual_listing_reader.wait_for_edit_form_ready'):
            result = read_listing_edit_visual(driver, folder, ocr=lambda path: [])
        self.assertFalse(result['ok'])
        self.assertTrue(result['warnings'])

    def test_non_edit_page_is_rejected_before_capture(self):
        driver = MagicMock(current_url='https://www.facebook.com/marketplace/')
        with self.assertRaises(ValueError):
            read_listing_edit_visual(driver, 'unused')
        driver.save_screenshot.assert_not_called()

    def test_ocr_failure_restores_scroll(self):
        import tempfile
        driver = MagicMock(current_url='https://www.facebook.com/marketplace/edit/?listing_id=123')
        scroll = object()
        driver.execute_script.side_effect = [scroll, 150, None, None]
        driver.save_screenshot.return_value = True
        with tempfile.TemporaryDirectory() as folder, patch('automation.visual_listing_reader.time.sleep'), patch('automation.visual_listing_reader.wait_for_edit_form_ready'):
            driver.execute_script.side_effect = [scroll, 150, None, None,
                {'price': {'value': '100', 'state': 'read'}, 'model': {'value': 'Civic', 'state': 'read'}},
                [], [0, 0, True], None]
            result = read_listing_edit_visual(driver, folder, ocr=MagicMock(side_effect=RuntimeError('OCR unavailable')))
        self.assertTrue(result['ok'])
        self.assertIn('OCR unavailable', result['warnings'][0])
        self.assertEqual(result['posts'][0]['field_states']['exterior_color'], 'not_detected')
        self.assertEqual(result['posts'][0]['field_states']['interior_color'], 'not_detected')
        self.assertEqual(driver.execute_script.call_args.args[1:], (scroll, 150))

    def test_both_colour_spellings(self):
        result = parse_ocr_fields([{'text': text, 'confidence': 95} for text in
                                  ['Exterior colour: Blue', 'Interior color: Black']])
        self.assertEqual(result['exterior_color']['value'], 'Blue')
        self.assertEqual(result['interior_color']['value'], 'Black')

    def test_optional_vision_failure_preserves_ocr(self):
        from automation.visual_listing_reader import recognition_proposals
        from pathlib import Path
        warnings = []
        lines, proposals = recognition_proposals(Path('form.png'),
            lambda path: [{'text': 'Year: 2020', 'confidence': 95}],
            MagicMock(side_effect=RuntimeError('provider unavailable')), warnings)
        self.assertEqual(proposals['year']['value'], '2020')
        self.assertIn('Vision unavailable', warnings[0])

    def test_repeated_control_reads_do_not_nest_evidence(self):
        fields = {'price': {'value': 'MADSl00', 'state': 'needs_review'}}
        for _ in range(12):
            merge_control_fields(fields, {'price': {'value': '100', 'state': 'read'}}, 'form.png')
        self.assertEqual(fields['price']['ocr_evidence']['value'], 'MADSl00')
        self.assertNotIn('ocr_evidence', fields['price']['ocr_evidence'])


if __name__ == '__main__':
    unittest.main()
