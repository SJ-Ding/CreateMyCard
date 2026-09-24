import { createContext, useCallback, useContext, useMemo, useReducer } from 'react';
import type { ReactNode } from 'react';
import type { CardArtifact, DebugEvent, WorkbenchContextValue } from './types';

type State = {
  events: DebugEvent[];
  artifact: CardArtifact | null;
};

type Action =
  | { type: 'event'; event: DebugEvent }
  | { type: 'artifact'; artifact: CardArtifact | null }
  | { type: 'clear-events' };

const initialState: State = { events: [], artifact: null };

function reducer(state: State, action: Action): State {
  if (action.type === 'event') {
    return { ...state, events: [...state.events.slice(-499), action.event] };
  }
  if (action.type === 'artifact') {
    return { ...state, artifact: action.artifact };
  }
  return { ...state, events: [] };
}

const WorkbenchContext = createContext<WorkbenchContextValue | null>(null);

export function WorkbenchProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const pushEvent = useCallback(
    (event: Omit<DebugEvent, 'id' | 'timestamp'> & { timestamp?: string }) => {
      dispatch({
        type: 'event',
        event: {
          ...event,
          id: crypto.randomUUID(),
          timestamp: event.timestamp ?? new Date().toISOString(),
        },
      });
    },
    [],
  );
  const value = useMemo<WorkbenchContextValue>(
    () => ({
      events: state.events,
      pushEvent,
      artifact: state.artifact,
      setArtifact: (artifact) => dispatch({ type: 'artifact', artifact }),
    }),
    [pushEvent, state.artifact, state.events],
  );
  return <WorkbenchContext.Provider value={value}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbench(): WorkbenchContextValue {
  const value = useContext(WorkbenchContext);
  if (!value) {
    throw new Error('useWorkbench 必须在 WorkbenchProvider 内调用');
  }
  return value;
}
