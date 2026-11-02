"""Regress the exact currency suffix failure recorded on the remote PC."""
from decimal import Decimal
import unittest

from automation.posting_numbers import numeric_input_value, parse_numeric_value
from automation.posting_review import normalize_field_value


class PostingNumberTests(unittest.TestCase):
    def test_moroccan_currency_suffix_dots_are_outside_the_amount(self):
        self.assertEqual(parse_numeric_value('MAD567,000'), Decimal('567000'))
        self.assertEqual(parse_numeric_value('567,000 د.م.'), Decimal('567000'))
        self.assertEqual(normalize_field_value('price', 'MAD567,000'),
                         normalize_field_value('price', '567,000 د.م.'))

    def test_currency_prefixes_symbols_and_punctuation_do_not_change_amount(self):
        for value in ('$15,500', 'USD 15,500.00', '€15,500.00', '£15,500',
                      'Rs.15,500', 'Rs. 15,500', 'د.م. 15,500', '15,500 د.م.'):
            with self.subTest(value=value):
                self.assertEqual(parse_numeric_value(value), Decimal('15500'))

    def test_different_and_duplicated_amounts_still_fail_comparison(self):
        expected = normalize_field_value('price', 'MAD567,000')
        self.assertNotEqual(expected, normalize_field_value('price', '567,001 د.م.'))
        self.assertNotEqual(expected, normalize_field_value('price', '567,000,567 د.م.'))
        self.assertEqual(parse_numeric_value('567,000,567 د.م.'), Decimal('567000567'))

    def test_decimal_fraction_is_preserved(self):
        for value, wanted in (('$15,500.25', '15500.25'), ('15 500,25 €', '15500.25'),
                              ('15.500,25 €', '15500.25'), ('12.345', '12.345'),
                              ('1234,5678', '1234.5678'), ('.50', '.5'), ('$.50', '.5')):
            with self.subTest(value=value):
                self.assertEqual(parse_numeric_value(value), Decimal(wanted))
        self.assertNotEqual(parse_numeric_value('$15,500.25'), parse_numeric_value('$15,500.26'))

    def test_numeric_input_contains_no_currency_or_grouping(self):
        for value, wanted in (('567,000 د.م.', '567000'), ('MAD567,000', '567000'),
                              ('$15,500.50', '15500.5'), ('0.00 MAD', '0'), ('-0', '0')):
            with self.subTest(value=value):
                self.assertEqual(numeric_input_value(value), wanted)

    def test_arabic_digits_and_separators(self):
        self.assertEqual(parse_numeric_value('٥٦٧٬٠٠٠ د.م.'), Decimal('567000'))
        self.assertEqual(parse_numeric_value('١٥٬٥٠٠٫٢٥ د.م.'), Decimal('15500.25'))
        self.assertEqual(parse_numeric_value('\u200f١٢٣\u200e'), Decimal('123'))

    def test_signs_and_zero_are_not_dropped(self):
        self.assertEqual(parse_numeric_value('-$15.50'), Decimal('-15.50'))
        self.assertEqual(parse_numeric_value('USD -15.50'), Decimal('-15.50'))
        self.assertEqual(parse_numeric_value('(15,500 USD)'), Decimal('-15500'))
        self.assertEqual(parse_numeric_value(0), Decimal('0'))
        self.assertNotEqual(normalize_field_value('price', '-100'), normalize_field_value('price', '100'))

    def test_disjoint_numbers_and_malformed_grouping_are_rejected(self):
        for value in ('100 USD 200', '100/200', '100 - 200', '1,23,456', '12.34.56',
                      '1,234,56', 'USD', '', None, True, 'NaN', 'Infinity', '1e3'):
            with self.subTest(value=value):
                self.assertIsNone(parse_numeric_value(value))
                with self.assertRaises(ValueError):
                    numeric_input_value(value)

    def test_identical_invalid_text_never_passes_numeric_review(self):
        self.assertNotEqual(normalize_field_value('price', 'not a number'),
                            normalize_field_value('price', 'not a number'))

    def test_mileage_grouping_is_preserved(self):
        self.assertEqual(normalize_field_value('mileage', '123,456'),
                         normalize_field_value('mileage', '123456'))
        self.assertEqual(parse_numeric_value("1'234'567"), Decimal('1234567'))
        self.assertEqual(parse_numeric_value('1.234.567'), Decimal('1234567'))


if __name__ == '__main__':
    unittest.main()
