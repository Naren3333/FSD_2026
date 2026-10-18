import { describe, expect, it } from 'vitest';
import type { Assessment, Evaluation, Evidence } from '../api/models';
import { EvidenceNames, assessmentsOf, topicNames, studentName, studentNames, total } from './names';

const evaluation = (id: string, assessment: string): Evaluation => ({
  id,
  assessment_id: assessment,
  classroom_id: 'c',
  student_id: 's',
  status: 'finalized',
  evidence: [],
});

describe('names', () => {
  it('prefers the full name, then the username, and skips students with neither', () => {
    expect(studentName({ id: 's1', username: 'ada', display_name: 'Ada Lovelace' })).toBe('Ada Lovelace');
    expect(studentName({ id: 's1', username: 'ada', display_name: '' })).toBe('ada');
    const names = studentNames([
      { id: 's1', username: 'ada', display_name: '' },
      { id: 's2', username: '', display_name: '' },
    ]);
    expect([...names]).toEqual([['s1', 'ada']]);
  });

  it('names topics by id, and finds a question prompt by assessment and question', () => {
    const topics = topicNames([
      { id: 'k1', name: 'Fractions' },
      { id: 't1', name: 'Number' },
    ]);
    expect([...topics]).toEqual([
      ['k1', 'Fractions'],
      ['t1', 'Number'],
    ]);
    const assessment: Assessment = {
      id: 'a1',
      classroom_id: 'c',
      title: 'Quiz',
      questions: [{ id: 'q1', prompt: 'What is 1/2 + 1/4?', type: 'numerical', options: [], topic_id: 'k1' }],
    };
    const assessments = new Map([['a1', assessment]]);
    const names = new EvidenceNames(
      () => assessments,
      (id) => topics.get(id) ?? '',
    );
    const grade = { assessment_id: 'a1' } as Evaluation;
    const evidence = (question_id: string, topic_id = 'k1') =>
      ({ question_id, topic_id, correct: true, awarded: 1, possible: 1 }) as Evidence;
    expect(names.title(grade)).toBe('Quiz');
    expect(names.prompt(grade, evidence('q1'))).toBe('What is 1/2 + 1/4?');
    expect(names.prompt(grade, evidence('q2'))).toBe('');
    expect(names.prompt({ assessment_id: 'a2' } as Evaluation, evidence('q1'))).toBe('');
    expect(names.topic(evidence('q1'))).toBe('Fractions');
    expect(names.topic(evidence('q1', 'gone'))).toBe('');
  });

  it('totals marks and loads each referenced assessment once', async () => {
    expect(
      total([
        { question_id: 'q1', topic_id: 'k', correct: true, awarded: 1, possible: 1 },
        { question_id: 'q2', topic_id: 'k', correct: false, awarded: 0, possible: 2 },
      ]),
    ).toEqual({ awarded: 1, possible: 3 });
    const asked: string[] = [];
    const loaded = await assessmentsOf(
      [evaluation('e1', 'a1'), evaluation('e2', 'a1'), evaluation('e3', 'a2')],
      (id) => {
        asked.push(id);
        return Promise.resolve({ id, classroom_id: 'c', title: id, questions: [] });
      },
    );
    expect(asked).toEqual(['a1', 'a2']);
    expect([...loaded.keys()]).toEqual(['a1', 'a2']);
  });
});
