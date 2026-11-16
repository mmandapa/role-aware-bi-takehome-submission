"""Local fixture chat. Launch on loopback."""
from contextlib import closing
import sqlite3
from uuid import uuid4

import streamlit as st

from agent import baseline, db, policy


def show(result):
    st.text(result.answer)
    if result.charts:
        for chart in result.charts:
            st.vega_lite_chart(chart)
    for report in result.released_results:
        st.caption(report['source'].replace('_', ' ').title())
        st.dataframe([{f"{key} ({report['units'][key]})" if report['units'][key] else key: value
                       for key, value in row.items()} for row in report['display_rows']], hide_index=True)
    if result.audit:
        with st.expander('Query activity'):
            scope = result.audit['scope']
            st.text(f"{scope['role']} · {scope['region'] or 'All permitted regions'}" if scope else 'No verified scope.')
            if result.report_summary:
                st.text(result.report_summary)
            for event in result.audit['queries']:
                if event['sql']:
                    st.code(event['sql'], language='sql')
                if result.status in ('ok', 'empty'):
                    st.caption(f"{event['rows_released']} rows returned")
                for notice in event['notices']:
                    st.text(notice)


def main():
    st.set_page_config(page_title='Fraud operations BI')
    st.title('Fraud operations BI')
    try:
        with closing(db.connect()) as conn:
            identities = {row['user_id']: f"{row['full_name']} · {row['role']}"
                          for row in conn.execute('SELECT user_id, full_name, role FROM users ORDER BY user_id')}
        if not identities:
            raise sqlite3.DatabaseError()
    except sqlite3.Error:
        st.error('The warehouse is unavailable. Run the explicit setup step and try again.')
        return
    actor = st.sidebar.selectbox('Demo identity', list(identities), format_func=identities.get)
    state = st.session_state
    if 'bi_session' not in state:
        state.bi_session = uuid4().hex
    try:
        scope = policy.resolve_scope(db.get_user(actor))
        capability = (scope.user_id, scope.role, scope.region, scope.active_only,
                      scope.sources, scope.customer_fields, policy.VERSION)
    except (LookupError, ValueError, sqlite3.Error):
        if 'bi_conversation' in state:
            baseline.clear_conversation(state.bi_conversation, session_id=state.bi_session)
        state.bi_turns = []
        st.error('Identity unavailable.')
        return
    if state.get('bi_actor_scope') != capability:
        if 'bi_conversation' in state:
            baseline.clear_conversation(state.bi_conversation, session_id=state.bi_session)
        state.bi_conversation, state.bi_turns = uuid4().hex, []
        state.bi_actor_scope = capability
    question = st.chat_input('Ask about the warehouse')
    if question:
        with st.spinner('Running report…'):
            result = baseline.run(question, actor, state.bi_conversation, session_id=state.bi_session)
        state.bi_turns = [*state.bi_turns, (question, result)][-16:]
    for question, result in state.bi_turns:
        with st.chat_message('user'):
            st.text(question)
        with st.chat_message('assistant'):
            show(result)


if __name__ == '__main__':
    main()
