import React, { useState, useEffect } from 'react'
import { AuthProvider, useAuth } from './contexts/AuthContext.jsx'
import LoginPage from './components/LoginPage.jsx'
import ChatInterface from './components/ChatInterface.jsx'
import SchemaERD from './components/SchemaERD.jsx'
import IngestTable from './components/IngestTable.jsx'
import KnowledgePanel from './components/KnowledgePanel.jsx'
import BusinessGlossary from './components/BusinessGlossary.jsx'
import DataLineage from './components/DataLineage.jsx'

const tabStyle = (active) => ({
  padding: '6px 16px',
  border: 'none',
  background: 'none',
  cursor: 'pointer',
  fontWeight: active ? 600 : 400,
  color: active ? '#2563eb' : '#555',
  borderBottom: active ? '2px solid #2563eb' : '2px solid transparent',
  fontSize: 14,
})

function PolyQLIcon() {
  return (
    <svg viewBox="0 0 32 32" width="32" height="32" xmlns="http://www.w3.org/2000/svg">
      <rect width="32" height="32" rx="7" fill="#2563eb" />
      <ellipse cx="16" cy="10.5" rx="7.5" ry="2.8" fill="white" opacity="0.95" />
      <rect x="8.5" y="10.5" width="15" height="10" fill="white" opacity="0.15" />
      <line x1="8.5" y1="10.5" x2="8.5" y2="20.5" stroke="white" strokeWidth="1.6" opacity="0.95" />
      <line x1="23.5" y1="10.5" x2="23.5" y2="20.5" stroke="white" strokeWidth="1.6" opacity="0.95" />
      <ellipse cx="16" cy="20.5" rx="7.5" ry="2.8" fill="white" opacity="0.95" />
      <polyline points="14.5,13.5 17.5,13.5 15,17 18,17" fill="none" stroke="#2563eb" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function AppContent() {
  const { user, logout } = useAuth()
  const [activeTab, setActiveTab]     = useState('query')
  const [providers, setProviders]     = useState([])
  const [schemaTables, setSchemaTables] = useState([])

  useEffect(() => {
    if (!user) return
    fetch('/api/providers')
      .then((r) => r.json())
      .then((data) => setProviders(data.providers ?? []))
      .catch(() => {})
  }, [user])

  useEffect(() => {
    if (!user) return
    if (activeTab === 'lineage') {
      fetch('/api/schema')
        .then((r) => r.json())
        .then((data) => setSchemaTables((data.tables ?? []).map((t) => t.name)))
        .catch(() => {})
    }
  }, [activeTab, user])

  // Redirect to login if not authenticated
  if (!user) return <LoginPage />

  return (
    <div style={{ maxWidth: '98vw', margin: '0 auto', padding: '0 24px' }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        borderBottom: '1px solid #e5e7eb', paddingBottom: 0, marginBottom: 0, paddingTop: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, paddingBottom: 10 }}>
          <PolyQLIcon />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <span style={{ fontSize: 20, fontWeight: 700, color: '#1e1e2e', letterSpacing: '-0.3px', lineHeight: 1.1 }}>
              Poly-QL
            </span>
            <span style={{ fontSize: 11, color: '#9ca3af', letterSpacing: '0.2px', lineHeight: 1 }}>
              natural language → SQL
            </span>
          </div>
        </div>

        <nav style={{ display: 'flex', gap: 2 }}>
          <button style={tabStyle(activeTab === 'query')}   onClick={() => setActiveTab('query')}>Query</button>
          <button style={tabStyle(activeTab === 'schema')}  onClick={() => setActiveTab('schema')}>Schema / ERD</button>
          <button style={tabStyle(activeTab === 'ingest')}  onClick={() => setActiveTab('ingest')}>Ingest Table</button>
          <button style={tabStyle(activeTab === 'lineage')} onClick={() => setActiveTab('lineage')}>Join Path</button>
          <button style={tabStyle(activeTab === 'glossary')} onClick={() => setActiveTab('glossary')}>Glossary</button>
          <button style={tabStyle(activeTab === 'knowledge')} onClick={() => setActiveTab('knowledge')}>Knowledge</button>
        </nav>

        {/* User info + logout */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, paddingBottom: 10 }}>
          <span style={{ fontSize: 13, color: '#6b7280' }}>
            {user.username}
          </span>
          <button
            onClick={logout}
            style={{
              background: 'none', border: '1px solid #e5e7eb', borderRadius: 6,
              padding: '4px 12px', fontSize: 12, color: '#6b7280', cursor: 'pointer',
            }}
          >
            Sign out
          </button>
        </div>
      </div>

      {/* Tab content */}
      <div style={{ paddingTop: 16 }}>
        {activeTab === 'query'   && <ChatInterface providers={providers} />}
        {activeTab === 'schema'  && <SchemaERD />}
        {activeTab === 'ingest'  && <IngestTable providers={providers} />}
        {activeTab === 'lineage' && <DataLineage tables={schemaTables} />}
        {activeTab === 'glossary' && <BusinessGlossary />}
        {activeTab === 'knowledge' && <KnowledgePanel />}
      </div>
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  )
}
