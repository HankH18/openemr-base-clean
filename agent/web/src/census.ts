/**
 * The clinician's census — the rounding list the UI starts a session with.
 *
 * The Co-Pilot API deals in patient ids only; display identity (name, bed,
 * service line) comes from the EMR census feed. In this build that feed is
 * a static roster; swap it for a live census endpoint without touching the
 * rounds/chat plumbing.
 */

export interface CensusEntry {
  id: number;
  name: string;
  /** First name, for physician-voiced copy ("Since you last saw Ernest"). */
  given: string;
  age: number;
  sex: 'M' | 'F';
  bed: string;
  service: string;
}

export const CLINICIAN_ID = 42;
export const CLINICIAN_LABEL = 'Dr. N. Ellery — Hospitalist';

export const CENSUS: CensusEntry[] = [
  { id: 1001, name: 'Ernest Vaughn', given: 'Ernest', age: 67, sex: 'M', bed: '12-A', service: 'NSTEMI — chest pain' },
  { id: 1002, name: 'Rosa Delgado', given: 'Rosa', age: 58, sex: 'F', bed: '07-B', service: 'Hyperkalemia on ACE inhibitor' },
  { id: 1003, name: 'Marcus Webb', given: 'Marcus', age: 41, sex: 'M', bed: '03-A', service: 'Cellulitis, day 2 antibiotics' },
  { id: 1004, name: 'June Okafor', given: 'June', age: 74, sex: 'F', bed: '15-C', service: 'Syncope workup — stable' },
  { id: 1005, name: 'Lillian Cho', given: 'Lillian', age: 82, sex: 'F', bed: '09-A', service: 'Pyelonephritis — sepsis watch' },
];

const byId = new Map(CENSUS.map((entry) => [entry.id, entry]));

export function censusEntry(id: number): CensusEntry | undefined {
  return byId.get(id);
}

export function patientName(id: number): string {
  return byId.get(id)?.name ?? `Patient ${id}`;
}

export function objectPronoun(id: number): 'him' | 'her' | 'them' {
  const entry = byId.get(id);
  if (!entry) {
    return 'them';
  }
  return entry.sex === 'M' ? 'him' : 'her';
}
