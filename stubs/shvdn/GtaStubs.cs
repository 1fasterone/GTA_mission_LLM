// ─────────────────────────────────────────────────────────────────────────────
// Compile-fidelity stubs for ScriptHookVDotNet3 (SHVDN3).
//
// NOT a runtime — every member is a no-op. These exist so generated missions
// can be compile-checked headlessly (the SHVDN SDK has no redistributable
// reference assemblies). Method args use `params object[]` where the exact
// SHVDN overload doesn't matter for error-catching: the gate's job is to catch
// syntax errors, undeclared identifiers, unknown members, and brace problems —
// the classes of failure LLM-authored fragments actually produce.
// ─────────────────────────────────────────────────────────────────────────────
#pragma warning disable 67   // events never raised

using System;

namespace GTA
{
    [AttributeUsage(AttributeTargets.Class)]
    public class ScriptAttributes : Attribute
    {
        public bool NoDefaultInstance { get; set; }
        public string Author { get; set; }
        public string SupportURL { get; set; }
    }

    public abstract class Script
    {
        public event EventHandler Tick;
        public event EventHandler Aborted;
        public int Interval { get; set; }
        public static void Wait(int ms) { }
    }

    public static class Game
    {
        public static Player Player => null;
        public static float LastFrameTime => 0f;
        public static bool IsKeyPressed(System.Windows.Forms.Keys key) => false;
    }

    public class Player
    {
        public Ped Character => null;
        public int WantedLevel { get; set; }
        public int Money { get; set; }
    }

    public abstract class Entity
    {
        public GTA.Math.Vector3 Position { get; set; }
        public float Heading { get; set; }
        public int Health { get; set; }
        public bool IsPersistent { get; set; }
        public bool IsPositionFrozen { get; set; }
        public bool IsInvincible { get; set; }
        public bool IsAlive => false;
        public bool IsDead => false;
        public int Handle => 0;
        public bool Exists() => false;
        public void Delete() { }
        public void MarkAsNoLongerNeeded() { }
        public Blip AddBlip() => null;
    }

    public class Ped : Entity
    {
        public TaskInvoker Task => null;
        public Vehicle CurrentVehicle => null;
        public bool IsInVehicle() => false;
        public bool IsInVehicle(Vehicle vehicle) => false;
        public void SetIntoVehicle(Vehicle vehicle, VehicleSeat seat) { }
        public bool IsWalking => false;
        public bool IsRunning => false;
    }

    public class Vehicle : Entity
    {
        public float Speed { get; set; }
        public bool IsDriveable => false;
    }

    public class Prop : Entity { }

    public class TaskInvoker
    {
        public void Combat(params object[] args) { }
        public void FightAgainst(params object[] args) { }
        public void GuardCurrentPosition(params object[] args) { }
        public void WanderAround(params object[] args) { }
        public void DriveTo(params object[] args) { }
        public void FollowToOffsetOfEntity(params object[] args) { }
        public void VehicleChase(params object[] args) { }
        public void FleeFrom(params object[] args) { }
        public void RunTo(params object[] args) { }
        public void GoTo(params object[] args) { }
        public void HandsUp(params object[] args) { }
        public void ClearAll() { }
        public void ClearAllImmediately() { }
    }

    public struct Model
    {
        public Model(string name) { }
        public Model(int hash) { }
        public bool IsValid => true;
        public bool IsLoaded => true;
        public void Request() { }
        public bool Request(int timeout) => true;
        // SHVDN's Model converts implicitly from hashes and names.
        public static implicit operator Model(string name) => new Model(name);
        public static implicit operator Model(int hash) => new Model(hash);
        public static implicit operator Model(PedHash hash) => new Model((int)hash);
    }

    public class Blip
    {
        public BlipSprite Sprite { get; set; }
        public BlipColor Color { get; set; }
        public float Scale { get; set; }
        public bool IsFlashing { get; set; }
        public bool IsShortRange { get; set; }
        public string Name { get; set; }
        public GTA.Math.Vector3 Position { get; set; }
        public bool Exists() => false;
        public void Delete() { }
    }

    public static class World
    {
        public static Ped CreatePed(Model model, GTA.Math.Vector3 position) => null;
        public static Ped CreatePed(Model model, GTA.Math.Vector3 position, float heading) => null;
        public static Vehicle CreateVehicle(Model model, GTA.Math.Vector3 position) => null;
        public static Vehicle CreateVehicle(Model model, GTA.Math.Vector3 position, float heading) => null;
        public static Prop CreateProp(Model model, GTA.Math.Vector3 position, bool dynamic, bool placeOnGround) => null;
        public static Blip CreateBlip(GTA.Math.Vector3 position) => null;
        public static void DrawMarker(params object[] args) { }
    }

    public enum BlipSprite { Standard, Waypoint, Race, PersonalVehicleCar }
    public enum BlipColor { White, Red, Green, Blue, Yellow, Purple, Orange }
    public enum MarkerType { UpsideDownCone, VerticalCylinder, ThickChevronUp, Checkered }
    public enum VehicleSeat { None = -3, Any = -2, Driver = -1, Passenger = 0, LeftRear = 1, RightRear = 2 }
    [Flags] public enum VehicleDrivingFlags : uint { None = 0 }
    public enum DrivingStyle { None = 0, Normal = 786603, Rushed = 1074528293, AvoidTraffic = 786468, AvoidTrafficExtremely = 6, SometimesOvertakeTraffic = 5, IgnoreLights = 2883621 }
    public enum FiringPattern { Default, FullAuto, BurstFire }
    public enum PedHash : uint { Generic, Business01AMM, Business02AMM, Cop01SMY, Cop01SFY, Marine01SMM, Dealer01SMY, Hillbilly01AMM, Trucker01SMM }
}

namespace GTA.UI
{
    public static class Notification
    {
        public static void PostTicker(string message, bool isImportant) { }
        public static void PostTicker(string message, bool isImportant, bool cycleBlinking) { }
    }

    public static class Screen
    {
        public static void ShowSubtitle(string message) { }
        public static void ShowSubtitle(string message, int duration) { }
        public static void ShowHelpTextThisFrame(string helpText) { }
    }
}

namespace GTA.Math
{
    public struct Vector3
    {
        public float X, Y, Z;
        public Vector3(float x, float y, float z) { X = x; Y = y; Z = z; }
        public static Vector3 Zero => new Vector3(0f, 0f, 0f);
        public float DistanceTo(Vector3 other) => 0f;
        public float DistanceTo2D(Vector3 other) => 0f;
        public Vector3 Normalized => this;
        public float Length() => 0f;
        public static Vector3 operator +(Vector3 a, Vector3 b) => new Vector3(a.X + b.X, a.Y + b.Y, a.Z + b.Z);
        public static Vector3 operator -(Vector3 a, Vector3 b) => new Vector3(a.X - b.X, a.Y - b.Y, a.Z - b.Z);
        public static Vector3 operator *(Vector3 a, float s) => new Vector3(a.X * s, a.Y * s, a.Z * s);
        public static Vector3 operator *(float s, Vector3 a) => a * s;
        public override string ToString() => $"({X}, {Y}, {Z})";
    }

    public struct Vector2
    {
        public float X, Y;
        public Vector2(float x, float y) { X = x; Y = y; }
        public static Vector2 Zero => new Vector2(0f, 0f);
        public float DistanceTo(Vector2 other) => 0f;
        public float Length() => 0f;
        public static Vector2 operator +(Vector2 a, Vector2 b) => new Vector2(a.X + b.X, a.Y + b.Y);
        public static Vector2 operator -(Vector2 a, Vector2 b) => new Vector2(a.X - b.X, a.Y - b.Y);
        public static Vector2 operator *(Vector2 a, float s) => new Vector2(a.X * s, a.Y * s);
        public override string ToString() => $"({X}, {Y})";
    }
}

namespace GTA.Native
{
    public enum Hash : ulong
    {
        GET_HASH_KEY,
        GET_PLAYER_WANTED_LEVEL,
        GIVE_WEAPON_TO_PED,
        PLAY_MISSION_COMPLETE_AUDIO,
        PLAY_SOUND_FRONTEND,
        SET_CLOCK_TIME,
        SET_WEATHER_TYPE_NOW_PERSIST,
        TASK_FOLLOW_TO_OFFSET_OF_ENTITY,
        SET_PLAYER_WANTED_LEVEL,
        PLACE_OBJECT_ON_GROUND_PROPERLY,
        SET_PLAYER_WANTED_LEVEL_NOW,
        SET_ENTITY_INVINCIBLE,
        SET_PED_AS_COP,
        TASK_VEHICLE_CHASE,
        TASK_GUARD_CURRENT_POSITION,
        TASK_LOOK_AT_COORD,
        TASK_STAND_STILL,
        ADD_BLIP_FOR_COORD,
        REQUEST_MODEL,
        SET_ENTITY_HEALTH,
        START_SCREEN_EFFECT,
        STOP_SCREEN_EFFECT,
        SHAKE_GAMEPLAY_CAM,
        SET_VEHICLE_ENGINE_ON,
        SET_VEHICLE_FORWARD_SPEED,
    }

    public static class Function
    {
        public static void Call(Hash hash, params object[] arguments) { }
        public static T Call<T>(Hash hash, params object[] arguments) => default;
    }
}
