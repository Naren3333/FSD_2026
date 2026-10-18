package main

import (
	"context"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"testing"

	"github.com/google/uuid"
)

// These tests run the store against real PostgreSQL, in a throwaway schema with every migration applied. They need
// IDENTITY_TEST_DATABASE_URL and are skipped without it.

func testStore(t *testing.T) pgStore {
	t.Helper()
	return pgStore{platformtest.DB(t, "IDENTITY_TEST_DATABASE_URL", migrations)}
}

// register registers the user, failing the test on an error.
func register(t *testing.T, s pgStore, org, subject, role, username string) {
	t.Helper()
	if err := s.registerUser(context.Background(), org, subject, role, username, username); err != nil {
		t.Fatalf("register %s: %v", subject, err)
	}
}

// user is the stored row of the account: its organization, role, username and display name.
func user(t *testing.T, s pgStore, subject string) [4]string {
	t.Helper()
	var v [4]string
	err := s.db.QueryRow(context.Background(), "SELECT org_id,role,COALESCE(username,''),COALESCE(display_name,'') FROM users WHERE id=$1", subject).Scan(&v[0], &v[1], &v[2], &v[3])
	if err != nil {
		t.Fatalf("user %s: %v", subject, err)
	}
	return v
}

// classroom creates a classroom of the teacher's and returns its id.
func classroom(t *testing.T, s pgStore, org, teacher string) string {
	t.Helper()
	id := uuid.NewString()
	if err := s.insertClassroom(context.Background(), id, "Year 5", org, teacher); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestMigrationsApply(t *testing.T) {
	s := testStore(t)
	var index bool
	if err := s.db.QueryRow(context.Background(), "SELECT to_regclass('users_org_role') IS NOT NULL").Scan(&index); err != nil || !index {
		t.Fatalf("users_org_role index missing: %v %v", index, err)
	}
}

func TestRegisterUserKeepsTheFirstRole(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "u1", "student", "ada")
	if got := user(t, s, "u1"); got != [4]string{"org-a", "student", "ada", "ada"} {
		t.Fatalf("first registration stored %v", got)
	}
	if err := s.registerUser(ctx, "org-a", "u1", "student", "ada2", "Ada L"); err != nil {
		t.Fatal(err)
	}
	if got := user(t, s, "u1"); got != [4]string{"org-a", "student", "ada2", "Ada L"} {
		t.Fatalf("names were not refreshed: %v", got)
	}
	if err := s.registerUser(ctx, "org-a", "u1", "teacher", "ada3", "Ada T"); !errors.Is(err, errRoleChanged) {
		t.Fatalf("role change answered %v", err)
	}
	if got := user(t, s, "u1"); got != [4]string{"org-a", "student", "ada2", "Ada L"} {
		t.Fatalf("a refused role change altered the account: %v", got)
	}
}

func TestRegisterUserInAnotherOrganizationChangesNothing(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "u1", "teacher", "lin")
	for _, role := range []string{"teacher", "student"} {
		if err := s.registerUser(ctx, "org-b", "u1", role, "other", "Other"); err != nil {
			t.Fatalf("%s in another organization: %v", role, err)
		}
	}
	if got := user(t, s, "u1"); got != [4]string{"org-a", "teacher", "lin", "lin"} {
		t.Fatalf("the account changed: %v", got)
	}
}

func TestStudentLookupsStayInTheOrganizationAndSkipTeachers(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "teacher-a", "teacher", "sam")
	register(t, s, "org-a", "student-a", "student", "kim")
	register(t, s, "org-b", "student-b", "student", "sam")
	register(t, s, "org-b", "teacher-b", "teacher", "kim")

	for _, tc := range []struct {
		student, org string
		want         bool
	}{{"student-a", "org-a", true}, {"student-b", "org-a", false}, {"teacher-a", "org-a", false}, {"student-a", "org-b", false}} {
		if got, err := s.studentExists(ctx, tc.student, tc.org); err != nil || got != tc.want {
			t.Errorf("studentExists(%s, %s) = %v, %v", tc.student, tc.org, got, err)
		}
	}
	// "sam" is a teacher in org-a and a student in org-b: neither is a student of org-a.
	if ids, err := s.studentsNamed(ctx, "org-a", "SAM"); err != nil || len(ids) != 0 {
		t.Errorf("studentsNamed sam = %v, %v", ids, err)
	}
	if ids, err := s.studentsNamed(ctx, "org-a", "Kim"); err != nil || len(ids) != 1 || ids[0] != "student-a" {
		t.Errorf("studentsNamed kim = %v, %v", ids, err)
	}
	students, total, err := s.listOrgStudents(ctx, "org-a", "", 0, 50)
	if err != nil || total != 1 || len(students) != 1 || students[0].ID != "student-a" {
		t.Errorf("listOrgStudents = %v, %d, %v", students, total, err)
	}
}

func TestListClassroomsStaysInTheCallersOrganization(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "teacher-a", "teacher", "a")
	register(t, s, "org-b", "teacher-b", "teacher", "b")
	register(t, s, "org-a", "student-a", "student", "s")
	mine := classroom(t, s, "org-a", "teacher-a")
	other := classroom(t, s, "org-b", "teacher-b")
	// An enrollment row in another organization's classroom (which enrollment never makes) is still not listed.
	for _, id := range []string{mine, other} {
		if err := s.insertEnrollment(ctx, id, "student-a"); err != nil {
			t.Fatal(err)
		}
	}
	if got, err := s.listClassrooms(ctx, "org-a", "teacher-a", true); err != nil || len(got) != 1 || got[0].ID != mine {
		t.Errorf("teacher's classrooms = %v, %v", got, err)
	}
	if got, err := s.listClassrooms(ctx, "org-a", "student-a", false); err != nil || len(got) != 1 || got[0].ID != mine {
		t.Errorf("student's classrooms = %v, %v", got, err)
	}
	// Owning a classroom gives nothing to a caller listing as a student.
	if got, err := s.listClassrooms(ctx, "org-a", "teacher-a", false); err != nil || len(got) != 0 {
		t.Errorf("non-teacher owner's classrooms = %v, %v", got, err)
	}
	if got, err := s.listClassrooms(ctx, "org-b", "teacher-a", true); err != nil || len(got) != 0 {
		t.Errorf("classrooms from another organization = %v, %v", got, err)
	}
}

func TestADeletedClassroomIsGoneAndAnnounced(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "teacher-a", "teacher", "a")
	register(t, s, "org-a", "student-a", "student", "s")
	id := classroom(t, s, "org-a", "teacher-a")
	if err := s.insertEnrollment(ctx, id, "student-a"); err != nil {
		t.Fatal(err)
	}
	if org, teacher, err := s.findClassroomOwner(ctx, id); err != nil || org != "org-a" || teacher != "teacher-a" {
		t.Fatalf("owner = %s %s %v", org, teacher, err)
	}
	for range 2 { // deleting twice announces once
		if err := s.deleteClassroom(ctx, id, "org-a"); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := s.findClassroomOwner(ctx, id); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("owner of a deleted classroom: %v", err)
	}
	if err := s.insertEnrollment(ctx, id, "student-a"); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("enrolling in a deleted classroom: %v", err)
	}
	if enrolled, err := s.isEnrolled(ctx, id, "student-a"); err != nil || enrolled {
		t.Errorf("still enrolled: %v %v", enrolled, err)
	}
	var topic, key, classroomID, org string
	var count int
	err := s.db.QueryRow(ctx, "SELECT count(*) OVER (),topic,partition_key,body->'data'->>'classroom_id',body->'data'->>'org_id' FROM outbox").Scan(&count, &topic, &key, &classroomID, &org)
	if err != nil || count != 1 || topic != platform.TopicClassroomDeleted || key != id || classroomID != id || org != "org-a" {
		t.Fatalf("outbox: %d %s %s %s %s %v", count, topic, key, classroomID, org, err)
	}
	if user(t, s, "student-a")[1] != "student" {
		t.Error("the student's account changed")
	}
}

func TestInsertClassroomNeedsARegisteredTeacher(t *testing.T) {
	s := testStore(t)
	register(t, s, "org-a", "teacher-a", "teacher", "a")
	if err := s.insertClassroom(context.Background(), uuid.NewString(), "Year 5", "org-a", "nobody"); !errors.Is(err, errProfileRequired) {
		t.Fatalf("unregistered teacher: %v", err)
	}
}

func TestRostersAndRemoval(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	register(t, s, "org-a", "teacher-a", "teacher", "a")
	register(t, s, "org-a", "student-b", "student", "bea")
	register(t, s, "org-a", "student-a", "student", "ann")
	id := classroom(t, s, "org-a", "teacher-a")
	for _, student := range []string{"student-b", "student-a", "student-a"} {
		if err := s.insertEnrollment(ctx, id, student); err != nil {
			t.Fatal(err)
		}
	}
	roster, err := s.listStudents(ctx, id)
	if err != nil || len(roster) != 2 || roster[0].ID != "student-a" || roster[1].Name != "bea" {
		t.Fatalf("roster = %v, %v", roster, err)
	}
	if removed, err := s.removeEnrollment(ctx, id, "org-a", "student-a"); err != nil || !removed {
		t.Fatalf("remove = %v, %v", removed, err)
	}
	if removed, err := s.removeEnrollment(ctx, id, "org-a", "student-a"); err != nil || removed {
		t.Fatalf("remove twice = %v, %v", removed, err)
	}
	// Announced once, for the classroom, naming the student.
	var events int
	var student, org string
	if err = s.db.QueryRow(ctx, "SELECT count(*) OVER (), body->'data'->>'student_id', body->'data'->>'org_id' FROM outbox WHERE topic=$1 AND partition_key=$2", platform.TopicEnrollmentRemoved, id).Scan(&events, &student, &org); err != nil || events != 1 || student != "student-a" || org != "org-a" {
		t.Fatalf("event = %d %s %s, %v", events, student, org, err)
	}
	if students, total, err := s.listOrgStudents(ctx, "org-a", "BE", 0, 10); err != nil || total != 1 || students[0].ID != "student-b" {
		t.Fatalf("search = %v, %d, %v", students, total, err)
	}
}

func TestADeletedAccountIsClearedAnnouncedAndNeverReturns(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	student, teacher := uuid.NewString(), uuid.NewString()
	register(t, s, "org-a", teacher, "teacher", "t")
	register(t, s, "org-a", student, "student", "ada")
	id := classroom(t, s, "org-a", teacher)
	if err := s.insertEnrollment(ctx, id, student); err != nil {
		t.Fatal(err)
	}
	if err := s.deleteAccount(ctx, "org-b", student); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("deleted from another organization: %v", err)
	}
	if err := s.deleteAccount(ctx, "org-a", teacher); !errors.Is(err, errOwnsClassrooms) {
		t.Fatalf("a teacher with a classroom: %v", err)
	}
	if err := s.deleteAccount(ctx, "org-a", student); err != nil {
		t.Fatal(err)
	}
	if err := s.deleteAccount(ctx, "org-a", student); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("deleted twice: %v", err)
	}
	if v := user(t, s, student); v[2] != "" || v[3] != "" {
		t.Errorf("names kept: %v", v)
	}
	if enrolled, err := s.isEnrolled(ctx, id, student); err != nil || enrolled {
		t.Errorf("still enrolled: %v %v", enrolled, err)
	}
	if err := s.registerUser(ctx, "org-a", student, "student", "ada", "Ada"); !errors.Is(err, errAccountDeleted) {
		t.Errorf("registered again: %v", err)
	}
	if err := s.insertEnrollment(ctx, id, student); !errors.Is(err, errStudentDeleted) {
		t.Errorf("enrolled again: %v", err)
	}
	if exists, err := s.studentExists(ctx, student, "org-a"); err != nil || exists {
		t.Errorf("still found: %v %v", exists, err)
	}
	var topic, key, data string
	if err := s.db.QueryRow(ctx, "SELECT topic,partition_key,body->>'data' FROM outbox WHERE topic=$1", platform.TopicUserDeleted).Scan(&topic, &key, &data); err != nil {
		t.Fatal(err)
	}
	if v, err := platform.DecodeUserDeleted(platform.Event{Resource: key, Data: []byte(data)}); err != nil || v.User != student || v.Org != "org-a" || v.Role != "student" {
		t.Fatalf("event %+v %v", v, err)
	}

	// Once the teacher's classroom is gone, the teacher can go too, and cannot create another.
	if err := s.deleteClassroom(ctx, id, "org-a"); err != nil {
		t.Fatal(err)
	}
	if err := s.deleteAccount(ctx, "org-a", teacher); err != nil {
		t.Fatal(err)
	}
	if err := s.insertClassroom(ctx, uuid.NewString(), "Year 6", "org-a", teacher); !errors.Is(err, errProfileRequired) {
		t.Errorf("a deleted teacher created a classroom: %v", err)
	}
}

// fakeKeycloak records the sign-in accounts removed, failing for those in fail.
type fakeKeycloak struct {
	removed []string
	fail    map[string]bool
}

func (f *fakeKeycloak) remove(_ context.Context, user string) error {
	if f.fail[user] {
		return errors.New("keycloak down")
	}
	f.removed = append(f.removed, user)
	return nil
}

func TestDeletedAccountsLoseTheirSignInOnce(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	first, second, kept := uuid.NewString(), uuid.NewString(), uuid.NewString()
	for _, id := range []string{first, second, kept} {
		register(t, s, "org-a", id, "student", "s")
	}
	for _, id := range []string{first, second} {
		if err := s.deleteAccount(ctx, "org-a", id); err != nil {
			t.Fatal(err)
		}
	}
	keycloak := &fakeKeycloak{fail: map[string]bool{second: true}}
	remover := signInCleanup{s.db, keycloak}
	if n, err := remover.removePending(ctx); err == nil || n != 1 {
		t.Fatalf("removed %d, %v", n, err)
	}
	keycloak.fail = nil
	for range 2 {
		if _, err := remover.removePending(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if len(keycloak.removed) != 2 || keycloak.removed[0] != first || keycloak.removed[1] != second {
		t.Fatalf("removed %v", keycloak.removed)
	}
}
