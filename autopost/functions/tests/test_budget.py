from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store, StateError


class BudgetTests(unittest.TestCase):
    def test_speech_limit(self):
        store = Store(FakeFirestore())
        store.reserve_speech(55*60,'source')
        with self.assertRaises(StateError):
            store.reserve_speech(1,'other')
        with self.assertRaises(StateError):
            store.reserve_speech(1,'source')


if __name__ == '__main__':
    unittest.main()
