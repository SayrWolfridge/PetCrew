import type { DemoAgent } from "./types";

export const LIST_DISCLOSURE_STORAGE_KEY = "petcrew.listDisclosure:v1";

const MAX_STORED_KEYS = 500;

export interface ListDisclosureState {
  collapsedProjects: ReadonlySet<string>;
  expandedCards: ReadonlySet<string>;
}

interface StoredListDisclosure {
  schema_version: 1;
  collapsed_projects: string[];
  expanded_cards: string[];
}

export function emptyListDisclosure(): ListDisclosureState {
  return {
    collapsedProjects: new Set<string>(),
    expandedCards: new Set<string>(),
  };
}

function validKeys(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => (
    typeof item === "string" && item.length > 0 && item.length <= 512
  )))].slice(0, MAX_STORED_KEYS);
}

export function parseListDisclosure(raw: string | null): ListDisclosureState {
  if (!raw) return emptyListDisclosure();
  try {
    const value = JSON.parse(raw) as Partial<StoredListDisclosure>;
    if (value.schema_version !== 1) return emptyListDisclosure();
    return {
      collapsedProjects: new Set(validKeys(value.collapsed_projects)),
      expandedCards: new Set(validKeys(value.expanded_cards)),
    };
  } catch {
    return emptyListDisclosure();
  }
}

export function serializeListDisclosure(state: ListDisclosureState): string {
  const value: StoredListDisclosure = {
    schema_version: 1,
    collapsed_projects: [...state.collapsedProjects].slice(0, MAX_STORED_KEYS),
    expanded_cards: [...state.expandedCards].slice(0, MAX_STORED_KEYS),
  };
  return JSON.stringify(value);
}

export function agentDisclosureKey(agent: DemoAgent): string {
  return agent.key ?? agent.agent_id;
}

export function toggleProjectDisclosure(
  state: ListDisclosureState,
  project: string,
): ListDisclosureState {
  const collapsedProjects = new Set(state.collapsedProjects);
  if (collapsedProjects.has(project)) collapsedProjects.delete(project);
  else collapsedProjects.add(project);
  return { ...state, collapsedProjects };
}

export function toggleCardDisclosure(
  state: ListDisclosureState,
  card: DemoAgent,
): ListDisclosureState {
  const key = agentDisclosureKey(card);
  const expandedCards = new Set(state.expandedCards);
  if (expandedCards.has(key)) expandedCards.delete(key);
  else expandedCards.add(key);
  return { ...state, expandedCards };
}
