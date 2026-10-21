// generated from contracts/http/identity.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Classroom: {
      id: string;
      name: string;
      teacher_id: string;
    };
    ClassroomPage: {
      items: components['schemas']['Classroom'][];
      total: number;
    };
    Enrollment: {
      student_id: string;
      classroom_id: string;
    };
    EnrollmentRequest: {
      student_id?: string;
      username?: string;
    } & (unknown | unknown);
    Error: {
      error: {
        code: string;
        message: string;
      };
    };
    Profile: {
      id: string;
      org_id: string;
      role: 'teacher' | 'student';
      username: string;
      display_name: string;
    };
    SchoolStudentPage: {
      students: components['schemas']['Student'][];
      total: number;
    };
    Student: {
      id: string;
      username: string;
      display_name: string;
    };
    StudentPage: {
      students: components['schemas']['Student'][];
      total: number;
    };
  };
  responses: never;
  parameters: never;
  requestBodies: never;
  headers: never;
  pathItems: never;
}
export type $defs = Record<string, never>;
export type operations = Record<string, never>;
