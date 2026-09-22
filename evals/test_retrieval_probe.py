import sqlite3
import unittest
from retrieval_probe import terms,initialize,build,indexed,exhaustive

class RetrievalProbe(unittest.TestCase):
    def test_chinese_words_case_numbers_and_repeated_terms(self):
        self.assertEqual(terms('PORT port 当前部署端口 17443'),{'port','当前','前部','部署','署端','端口','17443'})
        self.assertEqual(terms('中_A_B'),{'中','a','b'})

    def test_postings_keep_exact_weighted_order_scope_and_zero_fill(self):
        conn=sqlite3.connect(':memory:');initialize(conn)
        rows=[('a','a_noise','"archive port port port"'),('b','b_deployment_port','17443'),
              ('c','c_project','"当前部署端口"'),('d','d_archive','"deployment"')]
        build(conn,rows,'own')
        # Do not reuse build() IDs across scopes in this static probe.
        conn.execute('INSERT INTO docs VALUES (99,?,?,?,?)',('foreign','foreign','port','17443'))
        conn.execute('INSERT INTO postings VALUES (?,?,?,?)',('foreign','port',99,100))
        for query in ('port','deployment port','当前部署端口','archive','notpresent',''):
            for k in (1,2,10):
                self.assertEqual(indexed(conn,'own',query,k),exhaustive(rows,query,k))
        conn.close()

    def test_default_fts_tokenization_is_not_our_chinese_bigrams(self):
        conn=sqlite3.connect(':memory:')
        conn.execute('CREATE VIRTUAL TABLE c USING fts5(value)')
        conn.execute('INSERT INTO c VALUES (?)',('当前部署端口',))
        self.assertEqual(conn.execute('SELECT count(*) FROM c WHERE c MATCH ?',('端口',)).fetchone()[0],0)
        self.assertIn('端口',terms('当前部署端口'))
        conn.close()
