// generated from contracts/http/identity.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Account: {
      id: string;
      username: string;
      display_name: string;
      role: 'teacher' | 'student';
    };
    AccountInput: {
      role: 'teacher' | 'student';
      username: string;
      first_name: string;
      last_name: string;
    };
    AccountPage: {
      accounts: components['schemas']['Account'][];
      total: number;
    };
    Classroom: {
      id: string;
      name: string;
      teacher_id: string;
    };
    ClassroomPage: {
      items: components['schemas']['Classroom'][];
      total: number;
    };
    CreatedAccount: {
      id: string;
      username: string;
      display_name: string;
      role: 'teacher' | 'student';
      temporary_password: string;
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
      role: 'admin' | 'teacher' | 'student';
      username: string;
      display_name: string;
    };
    SchoolClassroom: {
      id: string;
      name: string;
      teacher_id: string;
      teacher_name: string;
      students: number;
    };
    SchoolClassroomPage: {
      items: components['schemas']['SchoolClassroom'][];
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
